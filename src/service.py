from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, plate_blockers


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind) if kind else None
        if field is None:
            return self.repository.list_entities(kind=kind)
        return self.repository.find_entities(kind, field, value)

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
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "plate" and action == "confirm_plate":
            return self._confirm_plate(actor, entity, data or {}, expected_version)
        return self._apply_transition(actor, entity, action, data, expected_version)

    def _apply_transition(self, actor, entity, action, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _confirm_plate(self, actor, plate, data, expected_version):
        expected = int(expected_version) if expected_version is not None else plate["version"]
        # This raises ConflictError with every blocker reason when the plate is not ready.
        _, patch = self.rules.validate_transition(
            actor, plate, "confirm_plate", dict(data or {}), self._lookup
        )
        updated_wells = [dict(well) for well in plate["data"].get("wells", [])]
        plate_code = plate["data"].get("plate_code")
        confirmed_at = patch.get("confirmed_at") or _utcnow()

        # Flip the plate first so its optimistic lock fails fast on stale submits.
        merged = dict(plate["data"])
        merged.update(patch)
        merged["wells"] = updated_wells
        confirmed_plate = self.repository.update_entity(
            plate["id"], expected, "confirmed", merged
        )
        self.audit.record(
            plate["id"], actor, "confirm_plate", plate["status"], "confirmed",
            {"plate_code": plate_code, "confirmed_at": confirmed_at},
        )

        # Propagate each sample well. Positives go to resulted with the plate code,
        # negatives close directly. Repeat confirmations keep the first result.
        for well in updated_wells:
            sample_id = well.get("sample_id")
            if not sample_id:
                continue
            sample = self.repository.get_entity(sample_id)
            if not sample:
                continue
            self._confirm_sample_well(actor, sample, well, plate_code, confirmed_at)
        return self.repository.get_entity(plate["id"])

    def _confirm_sample_well(self, actor, sample, well, plate_code, confirmed_at):
        result = well.get("result")
        sample_data = dict(sample["data"])
        first_result = sample_data.get("result")
        if first_result in ("positive", "negative"):
            # Repeat confirmation: keep the first recorded result, append history.
            effective = first_result
            history = list(sample_data.get("result_history", []))
            history.append({
                "plate_code": plate_code,
                "well": well.get("position"),
                "result": result,
                "result_at": confirmed_at,
                "repeated": True,
                "kept_result": first_result,
            })
            sample_data["result_history"] = history
            note = "复测沿用第一次结果 %s" % first_result
        else:
            effective = result
            sample_data["result"] = result
            sample_data["result_at"] = confirmed_at
            sample_data["result_plate_code"] = plate_code
            sample_data["result_well"] = well.get("position")
            note = "检测板 %s 孔位 %s 确认" % (plate_code, well.get("position"))

        plates = list(sample_data.get("plates", []))
        plates.append({"plate_code": plate_code, "well": well.get("position")})
        sample_data["plates"] = plates
        if effective == "positive" and not sample_data.get("first_positive_plate_code"):
            sample_data["first_positive_plate_code"] = plate_code

        next_status = "resulted" if effective == "positive" else "closed"
        updated_sample = self.repository.update_entity(
            sample["id"], sample["version"], next_status, sample_data
        )
        self.audit.record(
            sample["id"], actor, "plate_result", sample["status"], next_status,
            {"plate_code": plate_code, "well": well.get("position"),
             "result": result, "effective_result": effective, "note": note},
        )
        return updated_sample

    def plate_readiness(self, plate_id):
        plate = self.repository.get_entity(plate_id)
        if not plate:
            raise NotFoundError("entity not found: " + plate_id)
        samples = {sample["id"]: sample for sample in self.repository.list_entities(kind="sample")}
        blockers = plate_blockers(plate, samples)
        wells = []
        missing = []
        for well in plate["data"].get("wells", []):
            entry = dict(well)
            if well.get("sample_id"):
                sample = samples.get(well["sample_id"])
                if sample:
                    entry["sample_code"] = sample["data"].get("sample_code")
                    entry["observation_id"] = sample["data"].get("observation_id")
                    entry["sample_status"] = sample["status"]
            used = well.get("sample_id") or well.get("control")
            entry["used"] = bool(used)
            entry["has_result"] = well.get("result") in ("positive", "negative")
            if used and not entry["has_result"]:
                missing.append(well["position"])
            wells.append(entry)
        return {
            "id": plate["id"],
            "plate_code": plate["data"].get("plate_code"),
            "status": plate["status"],
            "rows": plate["data"].get("rows"),
            "cols": plate["data"].get("cols"),
            "wells": wells,
            "missing_results": missing,
            "blockers": blockers,
            "ready": not blockers,
        }

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
