from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .rules import RuleEngine, plate_summary


SYSTEM_ACTOR = Actor("system", "admin")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        if field == "status":
            return self.repository.list_entities(kind=kind, status=value)
        return self.repository.find_entities(kind, field, value)

    def _view(self, entity):
        if entity and entity["kind"] == "plate":
            data = dict(entity["data"])
            data["summary"] = plate_summary(entity)
            entity = dict(entity, data=data)
        return entity

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return self._view(entity)
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return self._view(entity)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        payload = dict(data or {})
        # 重复确认已确认的检测板沿用第一次结果，不再改动任何对象。
        if entity["kind"] == "plate" and entity["status"] == "confirmed" and action == "confirm":
            return self._view(entity)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        merged = dict(entity["data"])
        if entity["kind"] == "plate" and action == "set_well_result":
            position = patch["well"]
            result = patch["result"]
            wells = [dict(well) for well in merged.get("wells", [])]
            for well in wells:
                if well["well"] == position:
                    well["result"] = result
            merged["wells"] = wells
        else:
            merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch} if entity["kind"] != "plate" or action != "confirm" else {},
        )
        if entity["kind"] == "plate" and action == "confirm":
            self._apply_plate_outcomes(actor, updated, payload.get("result_at"))
        return self._view(updated)

    def _apply_plate_outcomes(self, actor, plate, result_at):
        """整板确认后：阳性样本转入已出结果，阴性样本关闭，并写回检测板号。"""
        plate_code = plate["data"].get("plate_code")
        for well in plate["data"].get("wells", []):
            if well.get("type") != "sample":
                continue
            sample = self.repository.get_entity(well["sample_id"])
            if not sample or sample["status"] != "in_lab":
                # 规则层已拦截，这里只做并发兜底。
                continue
            result = well["result"]
            result_data = {
                "result": result,
                "result_at": result_at,
                "plate_code": plate_code,
                "plate_id": plate["id"],
                "well": well["well"],
            }
            next_status, patch = self.rules.validate_transition(
                SYSTEM_ACTOR, sample, "lab_result", result_data, self._lookup,
                check_roles=False,
            )
            resulted = self.repository.update_entity(
                sample["id"], sample["version"], next_status,
                dict(sample["data"], **patch),
            )
            self.audit.record(
                sample["id"], actor, "lab_result", sample["status"], next_status,
                {"patch": patch, "plate_code": plate_code, "cascade": True},
            )
            if result == "negative":
                close_data = {"outcome": "negative", "plate_code": plate_code}
                next_status, patch = self.rules.validate_transition(
                    SYSTEM_ACTOR, resulted, "close", close_data, self._lookup,
                    check_roles=False,
                )
                self.repository.update_entity(
                    resulted["id"], resulted["version"], next_status,
                    dict(resulted["data"], **patch),
                )
                self.audit.record(
                    sample["id"], actor, "close", "resulted", next_status,
                    {"patch": patch, "plate_code": plate_code, "cascade": True},
                )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self._view(entity)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        return [self._view(item) for item in items] if kind == "plate" else items

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
