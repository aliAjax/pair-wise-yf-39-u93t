import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PlateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab = Actor("lab-1", "lab")
        self.viewer = Actor("viewer-1", "viewer")
        self.samples = {}
        self._prepare_samples()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_observation(self, code):
        return self.service.create(
            self.admin,
            "observation",
            {
                "event_id": "E-" + code,
                "species": "deer",
                "location": "North",
                "observed_at": "2026-04-01",
                "lat": 40.0,
                "lon": 116.0,
            },
        )

    def _send_sample(self, code):
        observation = self._create_observation(code)
        self.service.transition(
            self.admin, observation["id"], "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        sample = self.service.create(
            self.admin, "sample",
            {"observation_id": observation["id"], "sample_code": "W-" + code},
        )
        self.service.transition(
            self.admin, sample["id"], "send_lab", {"lab_id": "LAB-1"}
        )
        self.samples[code] = sample["id"]
        return sample["id"]

    def _prepare_samples(self):
        for code in ("1", "2", "3"):
            self._send_sample(code)

    def _create_plate(self, plate_code, wells=None, actor=None):
        wells = wells or [
            {"well": "A1", "type": "control", "control": "negative"},
            {"well": "A2", "type": "sample", "sample_id": self.samples["1"]},
            {"well": "B1", "type": "control", "control": "positive"},
            {"well": "B2", "type": "sample", "sample_id": self.samples["2"]},
        ]
        return self.service.create(
            actor or self.lab, "plate",
            {"plate_code": plate_code, "wells": wells},
        )

    def _fill_results(self, plate_id, results):
        for well, result in results.items():
            self.service.transition(
                self.lab, plate_id, "set_well_result",
                {"well": well, "result": result},
            )

    def test_plate_confirm_cascades_positive_and_negative(self):
        plate = self._create_plate("P-001")
        summary = plate["data"]["summary"]
        self.assertEqual(sorted(summary["missing"]), ["A1", "A2", "B1", "B2"])
        self.assertEqual(summary["blockers"], ["missing_results"])

        self._fill_results(
            plate["id"],
            {"A1": "negative", "A2": "positive", "B1": "positive", "B2": "negative"},
        )
        pending = self.service.get(plate["id"])
        self.assertEqual(pending["data"]["summary"]["missing"], [])
        self.assertTrue(pending["data"]["summary"]["ready"])

        confirmed = self.service.transition(
            self.lab, plate["id"], "confirm", {"result_at": "2026-04-06"}
        )
        self.assertEqual(confirmed["status"], "confirmed")

        positive = self.service.get(self.samples["1"])
        self.assertEqual(positive["status"], "resulted")
        self.assertEqual(positive["data"]["result"], "positive")
        self.assertEqual(positive["data"]["plate_code"], "P-001")

        negative = self.service.get(self.samples["2"])
        # 阴性样本整板确认后直接关闭。
        self.assertEqual(negative["status"], "closed")
        self.assertEqual(negative["data"]["result"], "negative")
        self.assertEqual(negative["data"]["outcome"], "negative")
        self.assertEqual(negative["data"]["plate_code"], "P-001")

    def test_duplicate_plate_code_is_blocked(self):
        self._create_plate("P-DUP")
        with self.assertRaises(ConflictError):
            self._create_plate("P-DUP")

    def test_sample_on_two_unfinished_plates_is_blocked(self):
        self._create_plate("P-A")
        with self.assertRaises(ConflictError):
            self._create_plate(
                "P-B",
                [
                    {"well": "A1", "type": "control", "control": "negative"},
                    {"well": "A2", "type": "sample", "sample_id": self.samples["1"]},
                ],
            )
        # 第一块确认后，已出结果/关闭的样本不再占用未完成板。
        first = self.service.list("plate", status="pending")[0]
        for well in first["data"]["wells"]:
            result = well.get("control") or (
                "positive" if well["sample_id"] == self.samples["1"] else "negative"
            )
            self._fill_results(first["id"], {well["well"]: result})
        self.service.transition(
            self.lab, first["id"], "confirm", {"result_at": "2026-04-06"}
        )
        # 复测后样本可以排上新板。
        positive = self.service.get(self.samples["1"])
        self.service.transition(
            self.lab, positive["id"], "retest", {"reason": "verify"}
        )
        second = self._create_plate(
            "P-B",
            [
                {"well": "A1", "type": "control", "control": "negative"},
                {"well": "A2", "type": "sample", "sample_id": self.samples["1"]},
            ],
        )
        self.assertEqual(second["status"], "pending")

    def test_confirm_blocked_when_results_missing_or_controls_invalid(self):
        plate = self._create_plate("P-002")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab, plate["id"], "confirm", {"result_at": "2026-04-06"}
            )
        # 对照无效（A1 阴性对照出阳性）时拦住确认。
        self._fill_results(
            plate["id"],
            {"A1": "positive", "A2": "positive", "B1": "positive", "B2": "negative"},
        )
        view = self.service.get(plate["id"])
        self.assertIn("invalid_controls", view["data"]["summary"]["blockers"])
        invalid = [c["well"] for c in view["data"]["summary"]["controls"] if not c["valid"]]
        self.assertEqual(invalid, ["A1"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab, plate["id"], "confirm", {"result_at": "2026-04-06"}
            )

    def test_repeated_confirm_keeps_first_result(self):
        plate = self._create_plate("P-003")
        self._fill_results(
            plate["id"],
            {"A1": "negative", "A2": "positive", "B1": "positive", "B2": "negative"},
        )
        first = self.service.transition(
            self.lab, plate["id"], "confirm", {"result_at": "2026-04-06"}
        )
        confirmed_version = first["version"]
        # 重复确认原样返回，不增加版本与审计。
        audit_before = len(self.service.audit_log(plate["id"]))
        second = self.service.transition(
            self.lab, plate["id"], "confirm", {"result_at": "2026-04-07"}
        )
        self.assertEqual(second["version"], confirmed_version)
        self.assertEqual(second["data"]["result_at"], "2026-04-06")
        self.assertEqual(len(self.service.audit_log(plate["id"])), audit_before)

    def test_plate_requires_lab_role(self):
        with self.assertRaises(PermissionDenied):
            self._create_plate("P-X", actor=self.viewer)

    def test_sample_must_be_in_lab_and_unique_wells(self):
        with self.assertRaises(ValidationError):
            self._create_plate(
                "P-004",
                [
                    {"well": "A1", "type": "control", "control": "negative"},
                    {"well": "A2", "type": "sample", "sample_id": "unknown-sample"},
                ],
            )
        with self.assertRaises(ValidationError):
            self._create_plate(
                "P-005",
                [
                    {"well": "A1", "type": "sample", "sample_id": self.samples["1"]},
                    {"well": "A1", "type": "sample", "sample_id": self.samples["2"]},
                ],
            )
        with self.assertRaises(ValidationError):
            self._create_plate(
                "P-006",
                [
                    {"well": "A1", "type": "sample", "sample_id": self.samples["1"]},
                    {"well": "A2", "type": "sample", "sample_id": self.samples["1"]},
                ],
            )
        with self.assertRaises(ValidationError):
            self._create_plate(
                "P-007",
                [{"well": "C1", "type": "control"}],
            )


if __name__ == "__main__":
    unittest.main()
