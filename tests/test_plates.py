import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
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

    def tearDown(self):
        self.tmp.cleanup()

    def _in_lab_sample(self, code, observed_at="2026-04-01"):
        obs = self.service.create(self.admin, "observation", {
            "event_id": "E-" + code, "species": "deer", "location": "North",
            "observed_at": observed_at, "lat": 40.0, "lon": 116.0,
        })
        self.service.transition(self.admin, obs["id"], "submit",
                                {"location": "North", "observed_at": observed_at})
        sample = self.service.create(self.admin, "sample",
                                     {"observation_id": obs["id"], "sample_code": code})
        self.service.transition(self.admin, sample["id"], "send_lab", {"lab_id": "LAB-1"})
        return self.service.get(sample["id"])

    def _filled_plate(self, code, samples, controls=(("H1", "positive"), ("H2", "negative"))):
        plate = self.service.create(self.lab, "plate", {"plate_code": code})
        wells = [{"position": "A%d" % (i + 1), "sample_id": sample["id"]}
                 for i, sample in enumerate(samples)]
        wells += [{"position": pos, "control": kind} for pos, kind in controls]
        self.service.transition(self.lab, plate["id"], "add_wells", {"wells": wells})
        self.service.transition(self.lab, plate["id"], "start_testing", {})
        return self.service.get(plate["id"])

    def test_duplicate_plate_code_is_blocked(self):
        self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        with self.assertRaises(ConflictError):
            self.service.create(self.lab, "plate", {"plate_code": "P-1"})

    def test_only_lab_role_can_create_plate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("f1", "field"), "plate", {"plate_code": "P-x"})

    def test_only_in_lab_samples_can_be_arranged(self):
        plate = self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        sample = self._in_lab_sample("W-1")
        # A sample that never went to the lab cannot be placed.
        obs = self.service.create(self.admin, "observation", {
            "event_id": "E-2", "species": "deer", "location": "North",
            "observed_at": "2026-04-02", "lat": 40.0, "lon": 116.0})
        self.service.transition(self.admin, obs["id"], "submit",
                                {"location": "North", "observed_at": "2026-04-02"})
        fresh = self.service.create(self.admin, "sample",
                                    {"observation_id": obs["id"], "sample_code": "W-2"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, plate["id"], "add_wells",
                                    {"wells": [{"position": "A1", "sample_id": fresh["id"]}]})
        # Unknown sample id is rejected too.
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, plate["id"], "add_wells",
                                    {"wells": [{"position": "A1", "sample_id": "missing"}]})
        # The valid in_lab sample is accepted.
        updated = self.service.transition(
            self.lab, plate["id"], "add_wells",
            {"wells": [{"position": "A1", "sample_id": sample["id"]}]})
        self.assertEqual(updated["data"]["wells"][0]["sample_id"], sample["id"])

    def test_sample_cannot_be_on_two_unfinished_plates(self):
        sample = self._in_lab_sample("W-1")
        first = self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        self.service.transition(self.lab, first["id"], "add_wells",
                                {"wells": [{"position": "A1", "sample_id": sample["id"]}]})
        second = self.service.create(self.lab, "plate", {"plate_code": "P-2"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.lab, second["id"], "add_wells",
                                    {"wells": [{"position": "A1", "sample_id": sample["id"]}]})

    def test_sample_can_reuse_on_a_new_plate_after_the_first_is_confirmed(self):
        sample = self._in_lab_sample("W-1")
        first = self._filled_plate("P-1", [sample])
        results = {"A1": "negative", "H1": "positive", "H2": "negative"}
        self.service.transition(self.lab, first["id"], "record_results", {"results": results})
        self.service.transition(self.lab, first["id"], "confirm_plate", {})
        # Negative sample was closed; a retest reopens it for a new unfinished plate.
        self.assertEqual(self.service.get(sample["id"])["status"], "closed")
        self.service.transition(self.lab, sample["id"], "retest", {"reason": "qc"})
        second = self.service.create(self.lab, "plate", {"plate_code": "P-2"})
        updated = self.service.transition(
            self.lab, second["id"], "add_wells",
            {"wells": [{"position": "B3", "sample_id": self.service.get(sample["id"])["id"]}]})
        self.assertEqual(updated["data"]["wells"][14]["position"], "B3")

    def test_cannot_occupy_same_well_twice_or_mix_sample_and_control(self):
        sample_a = self._in_lab_sample("W-1", "2026-04-01")
        sample_b = self._in_lab_sample("W-2", "2026-04-02")
        plate = self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        self.service.transition(self.lab, plate["id"], "add_wells",
                                {"wells": [{"position": "A1", "sample_id": sample_a["id"]}]})
        with self.assertRaises(ConflictError):
            self.service.transition(self.lab, plate["id"], "add_wells",
                                    {"wells": [{"position": "A1", "sample_id": sample_b["id"]}]})
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, plate["id"], "add_wells",
                                    {"wells": [{"position": "A2", "sample_id": sample_b["id"],
                                                "control": "positive"}]})
        # Re-sending the same sample to its own well is an idempotent no-op.
        same = self.service.transition(
            self.lab, plate["id"], "add_wells",
            {"wells": [{"position": "A1", "sample_id": sample_a["id"]}]})
        self.assertEqual(same["data"]["wells"][0]["sample_id"], sample_a["id"])

    def test_well_position_outside_layout_is_rejected(self):
        self._in_lab_sample("W-1")
        plate = self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, plate["id"], "add_wells",
                                    {"wells": [{"position": "Z9", "control": "positive"}]})

    def test_confirmation_requires_full_results_and_valid_controls(self):
        sample = self._in_lab_sample("W-1")
        plate = self._filled_plate("P-1", [sample])
        # Nothing recorded yet: readiness lists each missing well.
        readiness = self.service.plate_readiness(plate["id"])
        self.assertFalse(readiness["ready"])
        self.assertEqual(set(readiness["missing_results"]), {"A1", "H1", "H2"})
        self.assertIn("孔位 A1 缺失结果", readiness["blockers"])
        with self.assertRaises(ConflictError):
            self.service.transition(self.lab, plate["id"], "confirm_plate", {})

        # Positive control reads negative -> control invalid, confirmation still blocked.
        self.service.transition(self.lab, plate["id"], "record_results", {
            "results": {"A1": "positive", "H1": "negative", "H2": "negative"}})
        with self.assertRaises(ConflictError):
            self.service.transition(self.lab, plate["id"], "confirm_plate", {})

        # Negative control must also read negative.
        self.service.transition(self.lab, plate["id"], "record_results",
                                {"results": {"H1": "positive", "H2": "positive"}})
        with self.assertRaises(ConflictError):
            self.service.transition(self.lab, plate["id"], "confirm_plate", {})

        # Once controls agree and all wells have results, confirmation succeeds.
        self.service.transition(self.lab, plate["id"], "record_results",
                                {"results": {"H2": "negative"}})
        confirmed = self.service.transition(self.lab, plate["id"], "confirm_plate", {})
        self.assertEqual(confirmed["status"], "confirmed")

    def test_plate_without_any_control_cannot_confirm(self):
        sample = self._in_lab_sample("W-1")
        plate = self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        self.service.transition(self.lab, plate["id"], "add_wells",
                                {"wells": [{"position": "A1", "sample_id": sample["id"]}]})
        self.service.transition(self.lab, plate["id"], "start_testing", {})
        self.service.transition(self.lab, plate["id"], "record_results",
                                {"results": {"A1": "positive"}})
        with self.assertRaises(ConflictError):
            self.service.transition(self.lab, plate["id"], "confirm_plate", {})

    def test_confirmation_moves_positive_to_resulted_with_plate_code_and_closes_negative(self):
        pos_sample = self._in_lab_sample("W-1", "2026-04-01")
        neg_sample = self._in_lab_sample("W-2", "2026-04-02")
        plate = self._filled_plate("P-2026-007", [pos_sample, neg_sample])
        self.service.transition(self.lab, plate["id"], "record_results", {
            "results": {"A1": "positive", "A2": "negative",
                        "H1": "positive", "H2": "negative"}})
        self.service.transition(self.lab, plate["id"], "confirm_plate", {})

        positive = self.service.get(pos_sample["id"])
        negative = self.service.get(neg_sample["id"])
        self.assertEqual(positive["status"], "resulted")
        self.assertEqual(positive["data"]["result"], "positive")
        self.assertEqual(positive["data"]["result_plate_code"], "P-2026-007")
        self.assertEqual(positive["data"]["result_well"], "A1")
        self.assertEqual(negative["status"], "closed")
        self.assertEqual(negative["data"]["result"], "negative")

    def test_repeat_confirmation_keeps_first_result(self):
        sample = self._in_lab_sample("W-1")
        first = self._filled_plate("P-1", [sample])
        self.service.transition(self.lab, first["id"], "record_results",
                                {"results": {"A1": "positive", "H1": "positive", "H2": "negative"}})
        self.service.transition(self.lab, first["id"], "confirm_plate", {})

        # Retest onto a second plate that reads negative; first positive result must stand.
        self.service.transition(self.lab, sample["id"], "retest", {"reason": "qc"})
        second = self._filled_plate("P-2", [self.service.get(sample["id"])])
        self.service.transition(self.lab, second["id"], "record_results",
                                {"results": {"A1": "negative", "H1": "positive", "H2": "negative"}})
        self.service.transition(self.lab, second["id"], "confirm_plate", {})

        repeated = self.service.get(sample["id"])
        self.assertEqual(repeated["status"], "resulted")
        self.assertEqual(repeated["data"]["result"], "positive")
        self.assertEqual(repeated["data"]["result_plate_code"], "P-1")
        self.assertEqual(len(repeated["data"]["result_history"]), 1)
        self.assertEqual(repeated["data"]["result_history"][0]["plate_code"], "P-2")
        self.assertEqual(repeated["data"]["result_history"][0]["result"], "negative")
        self.assertEqual(repeated["data"]["result_history"][0]["kept_result"], "positive")

    def test_confirmation_only_allowed_from_testing(self):
        plate = self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.lab, plate["id"], "confirm_plate", {})
        self.service.transition(self.lab, plate["id"], "start_testing", {})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.lab, plate["id"], "add_wells",
                                    {"wells": [{"position": "A1", "control": "positive"}]})

    def test_results_cannot_be_recorded_on_an_empty_well_or_with_bad_value(self):
        sample = self._in_lab_sample("W-1")
        plate = self._filled_plate("P-1", [sample])
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, plate["id"], "record_results",
                                    {"results": {"B2": "positive"}})
        with self.assertRaises(ValidationError):
            self.service.transition(self.lab, plate["id"], "record_results",
                                    {"results": {"A1": "maybe"}})

    def test_abandoned_plate_releases_samples_for_new_plates(self):
        sample = self._in_lab_sample("W-1")
        first = self.service.create(self.lab, "plate", {"plate_code": "P-1"})
        self.service.transition(self.lab, first["id"], "add_wells",
                                {"wells": [{"position": "A1", "sample_id": sample["id"]}]})
        self.service.transition(self.lab, first["id"], "abandon", {})
        second = self.service.create(self.lab, "plate", {"plate_code": "P-2"})
        updated = self.service.transition(
            self.lab, second["id"], "add_wells",
            {"wells": [{"position": "A1", "sample_id": sample["id"]}]})
        self.assertEqual(updated["data"]["wells"][0]["sample_id"], sample["id"])

    def test_plate_code_is_required_and_layout_bounded(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.lab, "plate", {})
        with self.assertRaises(ValidationError):
            self.service.create(self.lab, "plate", {"plate_code": "P-big", "rows": 99, "cols": 12})


if __name__ == "__main__":
    unittest.main()
