from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _empty_wells(rows, cols):
    return [
        {"position": "%s%d" % (row, col)}
        for row in "ABCDEFGHIJKLMNOP"[:rows]
        for col in range(1, cols + 1)
    ]


def _normalize_layout(data):
    rows = int(data.get("rows", 8))
    cols = int(data.get("cols", 12))
    if rows < 1 or cols < 1 or rows > 16 or cols > 24:
        raise ValidationError("plate layout must stay within 1..16 rows and 1..24 columns")
    data["rows"] = rows
    data["cols"] = cols
    code = str(data.get("plate_code", "")).strip()
    if not code:
        raise ValidationError("plate_code is required")
    data["plate_code"] = code


def _validate_plate(actor, data, lookup):
    _normalize_layout(data)
    existing = lookup("plate", "plate_code", data["plate_code"]) or []
    if existing:
        raise ConflictError("板号已存在: " + data["plate_code"])
    data["wells"] = _empty_wells(data["rows"], data["cols"])


def _well_index(wells, position):
    for index, well in enumerate(wells):
        if well.get("position") == position:
            return index
    raise ValidationError("孔位不在板内: " + str(position))


def _on_other_unfinished_plate(lookup, plate_id, sample_id):
    for plate in lookup("plate", None, None) or []:
        if plate["id"] == plate_id or plate["status"] not in ("draft", "testing"):
            continue
        for well in plate["data"].get("wells", []):
            if well.get("sample_id") == sample_id:
                return plate["data"].get("plate_code", plate["id"])
    return None


def _validate_add_wells(actor, entity, data, lookup):
    wells = data.get("wells")
    if not isinstance(wells, list) or not wells:
        raise ValidationError("wells must be a non-empty list")
    updated = [dict(well) for well in entity["data"].get("wells", [])]
    positions_seen = {well.get("position") for well in updated if well.get("sample_id") or well.get("control")}
    for item in wells:
        position = item.get("position")
        if not position:
            raise ValidationError("each well needs a position")
        index = _well_index(updated, position)
        target = updated[index]
        sample_id = item.get("sample_id")
        control = item.get("control")
        if not sample_id and not control:
            raise ValidationError("孔位 %s 需要样本或对照标注" % position)
        if sample_id and control:
            raise ValidationError("孔位 %s 不能同时放样本和对照" % position)
        if control and control not in ("positive", "negative"):
            raise ValidationError("对照类型必须是 positive 或 negative")
        if position in positions_seen and (
            target.get("sample_id") != sample_id or target.get("control") != control
        ):
            raise ConflictError("孔位 %s 已被占用" % position)
        if sample_id:
            sample = _find_one(lookup, "sample", "id", sample_id)
            if not sample:
                raise ValidationError("样本不存在: " + str(sample_id))
            if sample["status"] != "in_lab":
                raise ValidationError(
                    "样本 %s 当前状态 %s，只有已送检样本可以排样"
                    % (sample_id, sample["status"])
                )
            other = _on_other_unfinished_plate(lookup, entity["id"], sample_id)
            if other:
                raise ConflictError(
                    "样本 %s 已排在未完成板 %s 上，不能同时进入两块未完成板"
                    % (sample_id, other)
                )
        target.clear()
        target["position"] = position
        if sample_id:
            target["sample_id"] = sample_id
        else:
            target["control"] = control
        positions_seen.add(position)
    return {"wells": updated}


def _validate_record_results(actor, entity, data, lookup):
    results = data.get("results")
    if not isinstance(results, dict) or not results:
        raise ValidationError("results must be a non-empty object of position -> positive/negative")
    updated = [dict(well) for well in entity["data"].get("wells", [])]
    for position, value in results.items():
        if str(value).lower() not in ("positive", "negative"):
            raise ValidationError("孔位 %s 结果必须是 positive 或 negative" % position)
        index = _well_index(updated, position)
        well = updated[index]
        if not well.get("sample_id") and not well.get("control"):
            raise ValidationError("孔位 %s 尚未排样" % position)
        well["result"] = str(value).lower()
    return {"wells": updated}


def plate_blockers(plate, samples_by_id=None):
    """整板确认前的阻塞原因：缺失结果、无对照、对照失效。"""
    samples_by_id = samples_by_id or {}
    blockers = []
    wells = plate["data"].get("wells", [])
    controls = [well for well in wells if well.get("control")]
    if not controls:
        blockers.append("板上没有对照孔")
    for well in wells:
        position = well.get("position")
        if well.get("sample_id") or well.get("control"):
            result = well.get("result")
            if result not in ("positive", "negative"):
                blockers.append("孔位 %s 缺失结果" % position)
                continue
        if well.get("control"):
            if well["control"] == "positive" and well.get("result") != "positive":
                blockers.append("孔位 %s 阳性对照未显阳性" % position)
            if well["control"] == "negative" and well.get("result") != "negative":
                blockers.append("孔位 %s 阴性对照未显阴性" % position)
        elif well.get("sample_id"):
            sample = samples_by_id.get(well["sample_id"])
            if sample and sample["status"] != "in_lab":
                blockers.append(
                    "样本 %s 当前状态 %s，不在已送检状态"
                    % (well["sample_id"], sample["status"])
                )
    return blockers


def _validate_confirm_plate(actor, entity, data, lookup):
    if lookup is None:
        return {"confirmed_at": _utcnow()}
    samples = {sample["id"]: sample for sample in (lookup("sample", None, None) or [])}
    blockers = plate_blockers(entity, samples)
    if blockers:
        raise ConflictError("整板尚不能确认: " + "；".join(blockers))
    return {"confirmed_at": _utcnow()}


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


CUSTOM_CREATE = {'observation': _validate_observation, 'sample': _validate_sample, 'plate': _validate_plate}
CUSTOM_TRANSITIONS = {
    ('sample', 'lab_result'): _validate_lab_result,
    ('plate', 'add_wells'): _validate_add_wells,
    ('plate', 'record_results'): _validate_record_results,
    ('plate', 'confirm_plate'): _validate_confirm_plate,
}


class RuleEngine:
    ALIASES = {'observations': 'observation', 'samples': 'sample', 'clusters': 'cluster', 'plates': 'plate'}
    INITIAL_STATUS = {'observation': 'captured', 'sample': 'collected', 'cluster': 'draft', 'plate': 'draft'}
    TRANSITIONS = {'observation': {'submit': (('captured',), 'submitted'), 'reject': (('submitted',), 'rejected'), 'link_sample': (('submitted',), 'sampled')}, 'sample': {'send_lab': (('collected',), 'in_lab'), 'lab_result': (('in_lab',), 'resulted'), 'retest': (('resulted', 'closed'), 'in_lab'), 'close': (('resulted',), 'closed')}, 'cluster': {'confirm_cluster': (('draft',), 'confirmed'), 'dismiss': (('draft',), 'dismissed')}, 'plate': {'start_testing': (('draft',), 'testing'), 'add_wells': (('draft',), 'draft'), 'record_results': (('testing',), 'testing'), 'confirm_plate': (('testing',), 'confirmed'), 'abandon': (('draft', 'testing'), 'abandoned')}}
    CREATE_REQUIRED = {'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'), 'sample': ('observation_id', 'sample_code'), 'cluster': ('region',), 'plate': ('plate_code',)}
    ACTION_REQUIRED = {('observation', 'submit'): ('location', 'observed_at'), ('observation', 'reject'): ('reason',), ('observation', 'link_sample'): ('sample_id',), ('sample', 'send_lab'): ('lab_id',), ('sample', 'lab_result'): ('result', 'result_at'), ('sample', 'retest'): ('reason',), ('sample', 'close'): ('outcome',), ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'), ('cluster', 'dismiss'): ('reason',), ('plate', 'add_wells'): ('wells',), ('plate', 'record_results'): ('results',)}
    CREATE_ROLES = {'observation': ('admin', 'field'), 'sample': ('admin', 'field'), 'cluster': ('admin', 'epidemiologist'), 'plate': ('admin', 'lab')}
    ROLE_ACTIONS = {'submit': ('admin', 'field'), 'reject': ('admin', 'epidemiologist'), 'link_sample': ('admin', 'field'), 'send_lab': ('admin', 'field'), 'lab_result': ('admin', 'lab'), 'retest': ('admin', 'lab'), 'close': ('admin', 'epidemiologist'), 'confirm_cluster': ('admin', 'epidemiologist'), 'dismiss': ('admin', 'epidemiologist'), 'start_testing': ('admin', 'lab'), 'add_wells': ('admin', 'lab'), 'record_results': ('admin', 'lab'), 'confirm_plate': ('admin', 'lab'), 'abandon': ('admin', 'lab')}

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

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
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
