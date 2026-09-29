import re
from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

WELL_RESULTS = ("positive", "negative")
PLATE_CONTROLS = ("positive", "negative")
PLATE_PENDING = "pending"
PLATE_CONFIRMED = "confirmed"


def _validate_observation(actor, data, lookup):
    rows = lookup("observation", "event_id", data.get("event_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("observed_at") == data.get("observed_at"):
            raise ConflictError("duplicate observation event")
    if not data.get("species"):
        raise ValidationError("species is required")


def _validate_sample(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] not in ("submitted", "sampled"):
        raise ValidationError("sample requires a submitted observation")


def _validate_lab_result(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "negative"):
        raise ValidationError("lab result must be positive or negative")


def _normalize_wells(raw):
    if not isinstance(raw, list) or not raw:
        raise ValidationError("wells is required and must be a non-empty list")
    wells = []
    seen_positions = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError("well %d must be an object" % index)
        position = str(item.get("well", "")).strip().upper()
        if not re.fullmatch(r"[A-Z]+[0-9]{1,3}", position):
            raise ValidationError("well %d has an invalid position" % index)
        if position in seen_positions:
            raise ValidationError("duplicate well position on plate: " + position)
        seen_positions.add(position)
        well_type = str(item.get("type", "sample")).lower()
        if well_type not in ("sample", "control"):
            raise ValidationError("well %s type must be sample or control" % position)
        well = {"well": position, "type": well_type}
        if well_type == "control":
            control = str(item.get("control", "")).lower()
            if control not in PLATE_CONTROLS:
                raise ValidationError(
                    "control well %s must be annotated positive or negative" % position
                )
            well["control"] = control
        else:
            sample_id = item.get("sample_id")
            if not sample_id:
                raise ValidationError("sample well %s requires sample_id" % position)
            well["sample_id"] = str(sample_id)
        wells.append(well)
    return wells


def _validate_plate(actor, data, lookup):
    plate_code = str(data.get("plate_code", "")).strip()
    if not plate_code:
        raise ValidationError("plate_code is required")
    if lookup and _find_one(lookup, "plate", "plate_code", plate_code):
        raise ConflictError("plate code already exists: " + plate_code)
    wells = _normalize_wells(data.get("wells"))
    # 同板内重复排样与同一样本同时落在两块未完成板上都要拦住。
    pending_sample_wells = {}
    for plate in (lookup("plate", "status", PLATE_PENDING) if lookup else []) or []:
        for well in plate["data"].get("wells", []):
            if well.get("type") == "sample":
                pending_sample_wells.setdefault(well["sample_id"], []).append(
                    (plate["data"].get("plate_code"), well["well"])
                )
    seen_samples = set()
    for well in wells:
        if well["type"] != "sample":
            continue
        sample_id = well["sample_id"]
        if sample_id in seen_samples:
            raise ValidationError(
                "sample %s is placed more than once on this plate" % sample_id
            )
        seen_samples.add(sample_id)
        sample = _find_one(lookup, "sample", "id", sample_id)
        if not sample:
            raise ValidationError("sample not found: " + sample_id)
        if sample["status"] != "in_lab":
            raise ValidationError(
                "sample %s is not in lab (status: %s)" % (sample_id, sample["status"])
            )
        other = pending_sample_wells.get(sample_id)
        if other:
            code, position = other[0]
            raise ConflictError(
                "sample %s is already on unfinished plate %s at %s"
                % (sample_id, code, position)
            )
    data["wells"] = wells


def _validate_plate_confirm(actor, entity, data, lookup):
    summary = plate_summary(entity)
    if summary["missing"]:
        raise ValidationError(
            "missing well results: " + ", ".join(summary["missing"])
        )
    invalid = [
        item["well"]
        for item in summary["controls"]
        if item["result"] in WELL_RESULTS and not item["valid"]
    ]
    if not summary["controls"]:
        raise ValidationError("plate has no control wells")
    if invalid:
        raise ValidationError(
            "control wells do not match their annotation: " + ", ".join(invalid)
        )
    if lookup:
        # 并发保护：确认时样本可能已被其他流程处理。
        for well in entity["data"].get("wells", []):
            if well.get("type") != "sample":
                continue
            sample = _find_one(lookup, "sample", "id", well["sample_id"])
            if not sample or sample["status"] != "in_lab":
                status = sample["status"] if sample else "missing"
                raise ConflictError(
                    "sample %s cannot be resulted (status: %s)"
                    % (well["sample_id"], status)
                )


def _validate_well_result(actor, entity, data, lookup):
    well = str(data.get("well", "")).strip().upper()
    positions = {item["well"]: item for item in entity["data"].get("wells", [])}
    if well not in positions:
        raise ValidationError("well %s does not exist on this plate" % well)
    result = str(data.get("result", "")).lower()
    if result not in WELL_RESULTS:
        raise ValidationError("well result must be positive or negative")
    return {"well": well, "result": result}


def plate_summary(entity):
    """派生视图：孔位、缺失结果、对照有效性与阻塞原因。"""
    wells = entity["data"].get("wells", [])
    missing = [well["well"] for well in wells if well.get("result") not in WELL_RESULTS]
    controls = []
    for well in wells:
        if well.get("type") != "control":
            continue
        result = well.get("result")
        controls.append(
            {
                "well": well["well"],
                "control": well.get("control"),
                "result": result if result in WELL_RESULTS else None,
                "valid": result == well.get("control") if result in WELL_RESULTS else False,
            }
        )
    blockers = list(missing and ["missing_results"])
    if not controls:
        blockers.append("missing_controls")
    elif any(
        item["result"] in WELL_RESULTS and not item["valid"] for item in controls
    ):
        blockers.append("invalid_controls")
    return {
        "well_count": len(wells),
        "resulted_count": len(wells) - len(missing),
        "missing": missing,
        "controls": controls,
        "blockers": blockers,
        "ready": not blockers and entity["status"] == PLATE_PENDING,
    }


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def is_cluster(observations, max_days=14, radius_km=10):
    if len(observations) < 3:
        return False
    points = observations[:3]
    same_window = all(
        abs(_date_ordinal(points[0].get("observed_at")) - _date_ordinal(item.get("observed_at"))) <= max_days
        for item in points[1:]
    )
    close = all(
        _haversine_km(points[0]["lat"], points[0]["lon"], item["lat"], item["lon"]) <= radius_km
        for item in points[1:]
    )
    return same_window and close


CUSTOM_CREATE = {
    'observation': _validate_observation,
    'sample': _validate_sample,
    'plate': _validate_plate,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'lab_result'): _validate_lab_result,
    ('plate', 'set_well_result'): _validate_well_result,
    ('plate', 'confirm'): _validate_plate_confirm,
}


class RuleEngine:
    ALIASES = {
        'observations': 'observation',
        'samples': 'sample',
        'clusters': 'cluster',
        'plates': 'plate',
    }
    INITIAL_STATUS = {
        'observation': 'captured',
        'sample': 'collected',
        'cluster': 'draft',
        'plate': PLATE_PENDING,
    }
    TRANSITIONS = {
        'observation': {
            'submit': (('captured',), 'submitted'),
            'reject': (('submitted',), 'rejected'),
            'link_sample': (('submitted',), 'sampled'),
        },
        'sample': {
            'send_lab': (('collected',), 'in_lab'),
            'lab_result': (('in_lab',), 'resulted'),
            'retest': (('resulted',), 'in_lab'),
            'close': (('resulted',), 'closed'),
        },
        'cluster': {
            'confirm_cluster': (('draft',), 'confirmed'),
            'dismiss': (('draft',), 'dismissed'),
        },
        'plate': {
            'set_well_result': ((PLATE_PENDING,), PLATE_PENDING),
            'confirm': ((PLATE_PENDING,), PLATE_CONFIRMED),
        },
    }
    CREATE_REQUIRED = {
        'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'),
        'sample': ('observation_id', 'sample_code'),
        'cluster': ('region',),
        'plate': ('plate_code', 'wells'),
    }
    ACTION_REQUIRED = {
        ('observation', 'submit'): ('location', 'observed_at'),
        ('observation', 'reject'): ('reason',),
        ('observation', 'link_sample'): ('sample_id',),
        ('sample', 'send_lab'): ('lab_id',),
        ('sample', 'lab_result'): ('result', 'result_at'),
        ('sample', 'retest'): ('reason',),
        ('sample', 'close'): ('outcome',),
        ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'),
        ('cluster', 'dismiss'): ('reason',),
        ('plate', 'set_well_result'): ('well', 'result'),
        ('plate', 'confirm'): ('result_at',),
    }
    CREATE_ROLES = {
        'observation': ('admin', 'field'),
        'sample': ('admin', 'field'),
        'cluster': ('admin', 'epidemiologist'),
        'plate': ('admin', 'lab'),
    }
    ROLE_ACTIONS = {
        'submit': ('admin', 'field'),
        'reject': ('admin', 'epidemiologist'),
        'link_sample': ('admin', 'field'),
        'send_lab': ('admin', 'field'),
        'lab_result': ('admin', 'lab'),
        'retest': ('admin', 'lab'),
        'close': ('admin', 'epidemiologist'),
        'confirm_cluster': ('admin', 'epidemiologist'),
        'dismiss': ('admin', 'epidemiologist'),
        'set_well_result': ('admin', 'lab'),
        'confirm': ('admin', 'lab'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None, check_roles=True):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        if check_roles:
            allowed_roles = self.ROLE_ACTIONS.get(
                (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
            )
            self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
