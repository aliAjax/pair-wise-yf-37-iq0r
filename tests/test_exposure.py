import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def fixed_clock(value):
    moment = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return lambda: moment


class ExposureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(
            self.repo, RuleEngine(), clock=fixed_clock("2026-03-20T08:00:00")
        )
        self.admin = Actor("admin", "admin")
        self.investigator = Actor("investigator-7", "investigator")

    def tearDown(self):
        self.tmp.cleanup()

    def _confirmed_case(self, person_id="P-1", onset_date="2026-03-01"):
        case = self.service.create(
            self.admin, "case",
            {"person_id": person_id, "onset_date": onset_date,
             "location": "District-A", "symptoms": ["fever"]},
        )
        self.service.transition(
            self.admin, case["id"], "triage", {"clinician": "C-1"}
        )
        self.service.transition(
            self.admin, case["id"], "lab_positive",
            {"lab_id": "L-1", "result": "positive"},
        )
        return self.service.get(case["id"])

    def _register(self, case, contacts, owner="investigator-7", **extra):
        data = {
            "venue": "星光餐厅",
            "window_start": "2026-03-01T18:00",
            "window_end": "2026-03-01T22:00",
            "owner": owner,
            "due_at": "2026-03-15",
            "contacts": contacts,
        }
        data.update(extra)
        return self.service.transition(
            self.investigator, case["id"], "register_exposure", data
        )

    def test_register_exposure_creates_followup_per_person(self):
        case = self._confirmed_case()
        self._register(case, [
            {"person_id": "P-2", "phone": "13800000002",
             "exposure_start": "2026-03-01T19:00"},
            {"person_id": "P-3", "exposure_start": "2026-03-01T20:00"},
        ])
        contacts = self.service.list("contact")
        self.assertEqual(len(contacts), 2)
        by_person = {item["data"]["person_id"]: item for item in contacts}
        contact = by_person["P-2"]
        self.assertEqual(contact["status"], "identified")
        self.assertEqual(contact["data"]["case_id"], case["id"])
        self.assertEqual(contact["data"]["owner"], "investigator-7")
        self.assertEqual(contact["data"]["due_at"], "2026-03-15")
        self.assertEqual(contact["data"]["venue"], "星光餐厅")
        self.assertEqual(contact["data"]["exposure_start"], "2026-03-01T19:00")
        exposures = self.service.list("exposure")
        self.assertEqual(len(exposures), 1)
        self.assertEqual(
            self.service.get(case["id"])["data"]["exposure_ids"],
            [exposures[0]["id"]],
        )
        self.assertIn(exposures[0]["id"], contact["data"]["exposure_ids"])

    def test_same_person_repeated_in_roster_updates_phone_keeps_first_exposure(self):
        case = self._confirmed_case()
        self._register(case, [
            {"person_id": "P-2", "phone": "13800000002",
             "exposure_start": "2026-03-01T20:00"},
            {"person_id": "P-2", "phone": "13900000009",
             "exposure_start": "2026-03-01T18:30"},
        ])
        contacts = self.service.list("contact")
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["data"]["exposure_start"], "2026-03-01T18:30")
        self.assertEqual(contacts[0]["data"]["phone"], "13900000009")
        roster = {item["person_id"]: item
                  for item in self.service.list("exposure")[0]["data"]["contacts"]}
        self.assertEqual(roster["P-2"]["attendance_count"], 2)

    def test_roster_update_refreshes_contact_and_links_activity(self):
        case = self._confirmed_case()
        self._register(case, [
            {"person_id": "P-2", "phone": "13800000002",
             "exposure_start": "2026-03-01T19:00"},
        ])
        exposure = self.service.list("exposure")[0]
        self.service.transition(
            self.investigator, exposure["id"], "update_roster",
            {"contacts": [
                {"person_id": "P-2", "phone": "13700000007"},
                {"person_id": "P-4", "exposure_start": "2026-03-01T21:00"},
            ]},
        )
        contacts = {item["data"]["person_id"]: item
                    for item in self.service.list("contact")}
        self.assertEqual(set(contacts), {"P-2", "P-4"})
        p2 = contacts["P-2"]
        self.assertEqual(p2["data"]["phone"], "13700000007")
        # first exposure time is preserved
        self.assertEqual(p2["data"]["exposure_start"], "2026-03-01T19:00")
        # same activity must not be linked twice
        self.assertEqual(p2["data"]["exposure_ids"], [exposure["id"]])

    def test_completed_followup_is_not_regenerated(self):
        case = self._confirmed_case()
        self._register(case, [
            {"person_id": "P-2", "phone": "13800000002",
             "exposure_start": "2026-03-01T19:00"},
        ])
        contact = self.service.list("contact")[0]
        self.service.transition(
            self.investigator, contact["id"], "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-15"},
        )
        self.service.transition(
            self.investigator, contact["id"], "complete_followup",
            {"outcome": "no symptoms"},
        )
        exposure = self.service.list("exposure")[0]
        self.service.transition(
            self.investigator, exposure["id"], "update_roster",
            {"contacts": [
                {"person_id": "P-2", "phone": "13900000009"},
                {"person_id": "P-5", "exposure_start": "2026-03-01T21:00"},
            ]},
        )
        contacts = self.service.list("contact")
        self.assertEqual(len(contacts), 2)
        p2 = [item for item in contacts if item["data"]["person_id"] == "P-2"][0]
        self.assertEqual(p2["status"], "completed")
        # completed record is left untouched (phone not overwritten)
        self.assertEqual(p2["data"]["phone"], "13800000002")

    def test_closed_case_blocks_new_items_but_records_remain(self):
        case = self._confirmed_case()
        self._register(case, [
            {"person_id": "P-2", "exposure_start": "2026-03-01T19:00"},
        ])
        exposure_id = self.service.list("exposure")[0]["id"]
        self.service.transition(
            self.admin, case["id"], "recover", {"recovered_at": "2026-03-10"}
        )
        self.service.transition(
            self.admin, case["id"], "close", {"outcome": "recovered"}
        )
        with self.assertRaises(InvalidTransition):
            self._register(self.service.get(case["id"]), [
                {"person_id": "P-9", "exposure_start": "2026-03-01T19:00"},
            ])
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.investigator, exposure_id, "update_roster",
                {"contacts": [{"person_id": "P-9"}]},
            )
        # old records remain queryable
        self.assertEqual(len(self.service.list("exposure")), 1)
        self.assertEqual(len(self.service.list("contact")), 1)

    def test_register_only_allowed_after_confirmation(self):
        case = self.service.create(
            self.admin, "case",
            {"person_id": "P-1", "onset_date": "2026-03-01",
             "location": "District-A", "symptoms": ["fever"]},
        )
        with self.assertRaises(InvalidTransition):
            self._register(case, [
                {"person_id": "P-2", "exposure_start": "2026-03-01T19:00"},
            ])

    def test_viewer_cannot_register(self):
        case = self._confirmed_case()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"), case["id"], "register_exposure",
                {"venue": "X", "window_start": "2026-03-01T18:00",
                 "window_end": "2026-03-01T22:00",
                 "contacts": [{"person_id": "P-2"}]},
            )

    def test_exposure_cannot_be_created_directly(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin, "exposure",
                {"case_id": "C-1", "venue": "X"},
            )

    def test_invalid_window_rejected(self):
        case = self._confirmed_case()
        with self.assertRaises(ValidationError):
            self._register(case, [{"person_id": "P-2"}],
                           window_start="2026-03-01T22:00",
                           window_end="2026-03-01T18:00")

    def test_duty_board_filters_by_owner_and_lists_overdue(self):
        case = self._confirmed_case()
        self._register(case, [
            {"person_id": "P-2", "exposure_start": "2026-03-01T19:00"},
        ], owner="investigator-7")
        case_b = self._confirmed_case(person_id="P-7", onset_date="2026-03-02")
        self.service.transition(
            self.investigator, case_b["id"], "register_exposure",
            {"venue": "云端KTV", "window_start": "2026-03-02T19:00",
             "window_end": "2026-03-02T23:00", "owner": "investigator-8",
             "due_at": "2026-03-18",
             "contacts": [{"person_id": "P-8",
                           "exposure_start": "2026-03-02T20:00"}]},
        )
        board = self.service.duty_board(as_of="2026-03-20")
        venues = {item["venue"]: item for item in board["venues"]}
        self.assertEqual(venues["星光餐厅"]["open_count"], 1)
        self.assertEqual(len(venues["星光餐厅"]["overdue"]), 1)
        self.assertEqual(venues["云端KTV"]["open_count"], 1)
        overdue_people = {item["person_id"] for item in board["overdue"]}
        self.assertEqual(overdue_people, {"P-2", "P-8"})

        mine = self.service.duty_board(owner="investigator-7", as_of="2026-03-20")
        self.assertEqual(len(mine["venues"]), 1)
        self.assertEqual(mine["venues"][0]["venue"], "星光餐厅")
        self.assertEqual([item["person_id"] for item in mine["overdue"]], ["P-2"])

    def test_completed_items_disappear_from_duty_board(self):
        case = self._confirmed_case()
        self._register(case, [
            {"person_id": "P-2", "exposure_start": "2026-03-01T19:00"},
        ])
        contact = self.service.list("contact")[0]
        self.service.transition(
            self.investigator, contact["id"], "begin_followup",
            {"followup_start": "2026-03-02", "due_at": "2026-03-15"},
        )
        self.service.transition(
            self.investigator, contact["id"], "complete_followup",
            {"outcome": "no symptoms"},
        )
        board = self.service.duty_board(as_of="2026-03-20")
        self.assertEqual(board["venues"], [])
        self.assertEqual(board["overdue"], [])


if __name__ == "__main__":
    unittest.main()
