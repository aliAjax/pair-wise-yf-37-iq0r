import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class VenueFollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.investigator = Actor("inv-1", "investigator")

    def tearDown(self):
        self.tmp.cleanup()

    def _confirmed_case(self, person_id="P-1"):
        case = self.service.create(
            self.admin,
            "case",
            {
                "person_id": person_id,
                "onset_date": "2026-03-01",
                "location": "District-A",
                "symptoms": ["fever"],
            },
        )
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        self.service.transition(
            self.admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"}
        )
        return self.service.get(case["id"])

    def _venue(self, case_id, owner="INV-1", name="食堂"):
        return self.service.create(
            self.investigator,
            "venue",
            {
                "case_id": case_id,
                "name": name,
                "exposure_start": "2026-02-20",
                "exposure_end": "2026-02-22",
                "owner_id": owner,
            },
        )

    def test_venue_requires_confirmed_case(self):
        case = self.service.create(
            self.admin,
            "case",
            {
                "person_id": "P-1",
                "onset_date": "2026-03-01",
                "location": "District-A",
                "symptoms": ["fever"],
            },
        )
        with self.assertRaises(ValidationError):
            self._venue(case["id"])
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        with self.assertRaises(ValidationError):
            self._venue(case["id"])
        self.service.transition(
            self.admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"}
        )
        venue = self._venue(case["id"])
        self.assertEqual(venue["status"], "open")

    def test_venue_rejects_unknown_case_and_bad_window(self):
        with self.assertRaises(ValidationError):
            self._venue("missing-case")
        case = self._confirmed_case()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.investigator,
                "venue",
                {
                    "case_id": case["id"],
                    "name": "食堂",
                    "exposure_start": "2026-02-22",
                    "exposure_end": "2026-02-20",
                    "owner_id": "INV-1",
                },
            )

    def test_register_attendee_creates_followup_item(self):
        case = self._confirmed_case()
        venue = self._venue(case["id"])
        contact = self.service.register_attendee(
            self.investigator,
            venue["id"],
            {"person_id": "P-2", "contact_info": "138-0000"},
        )
        self.assertEqual(contact["kind"], "contact")
        self.assertEqual(contact["status"], "identified")
        self.assertEqual(contact["data"]["case_id"], case["id"])
        self.assertEqual(contact["data"]["venue_id"], venue["id"])
        self.assertEqual(contact["data"]["owner_id"], "INV-1")
        self.assertEqual(contact["data"]["exposure_start"], "2026-02-20")
        self.assertEqual(contact["data"]["due_at"], "2026-03-08")

    def test_duplicate_attendee_updates_contact_info_only(self):
        case = self._confirmed_case()
        venue = self._venue(case["id"])
        first = self.service.register_attendee(
            self.investigator,
            venue["id"],
            {"person_id": "P-2", "contact_info": "138-0000"},
        )
        again = self.service.register_attendee(
            self.investigator,
            venue["id"],
            {
                "person_id": "P-2",
                "contact_info": "139-1111",
                "exposure_at": "2026-02-21",
            },
        )
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["data"]["contact_info"], "139-1111")
        self.assertEqual(again["data"]["exposure_start"], "2026-02-20")
        contacts = self.service.list("contact")
        self.assertEqual(len(contacts), 1)

    def test_completed_followup_is_not_regenerated(self):
        case = self._confirmed_case()
        venue = self._venue(case["id"])
        contact = self.service.register_attendee(
            self.investigator,
            venue["id"],
            {"person_id": "P-2", "contact_info": "138-0000"},
        )
        self.service.transition(
            self.investigator,
            contact["id"],
            "begin_followup",
            {"followup_start": "2026-02-23", "due_at": "2026-03-08"},
        )
        self.service.transition(
            self.investigator,
            contact["id"],
            "complete_followup",
            {"outcome": "no symptoms"},
        )
        again = self.service.register_attendee(
            self.investigator,
            venue["id"],
            {"person_id": "P-2", "contact_info": "139-1111"},
        )
        self.assertEqual(again["id"], contact["id"])
        self.assertEqual(again["status"], "completed")
        self.assertEqual(again["data"]["contact_info"], "138-0000")
        self.assertEqual(len(self.service.list("contact")), 1)

    def test_closed_case_blocks_new_items_but_records_stay_readable(self):
        case = self._confirmed_case()
        venue = self._venue(case["id"])
        contact = self.service.register_attendee(
            self.investigator,
            venue["id"],
            {"person_id": "P-2", "contact_info": "138-0000"},
        )
        self.service.transition(
            self.admin, case["id"], "recover", {"recovered_at": "2026-03-10"}
        )
        self.service.transition(self.admin, case["id"], "close", {"outcome": "recovered"})
        with self.assertRaises(InvalidTransition):
            self.service.register_attendee(
                self.investigator,
                venue["id"],
                {"person_id": "P-3", "contact_info": "137-0000"},
            )
        with self.assertRaises(ValidationError):
            self._venue(case["id"], name="超市")
        self.assertEqual(self.service.get(contact["id"])["data"]["person_id"], "P-2")
        attendees = self.service.list_attendees(venue["id"])
        self.assertEqual([item["id"] for item in attendees], [contact["id"]])

    def test_register_attendee_rejects_unknown_venue_and_viewer(self):
        with self.assertRaises(NotFoundError):
            self.service.register_attendee(
                self.investigator, "missing-venue", {"person_id": "P-2", "contact_info": "x"}
            )
        case = self._confirmed_case()
        venue = self._venue(case["id"])
        with self.assertRaises(PermissionDenied):
            self.service.register_attendee(
                Actor("viewer", "viewer"),
                venue["id"],
                {"person_id": "P-2", "contact_info": "x"},
            )

    def test_duty_board_counts_and_overdue_by_owner(self):
        case = self._confirmed_case()
        venue_a = self._venue(case["id"], owner="INV-1", name="食堂")
        venue_b = self._venue(case["id"], owner="INV-2", name="超市")
        self.service.register_attendee(
            self.investigator, venue_a["id"], {"person_id": "P-2", "contact_info": "a"}
        )
        self.service.register_attendee(
            self.investigator,
            venue_a["id"],
            {"person_id": "P-3", "contact_info": "b", "due_at": "2026-01-01"},
        )
        done = self.service.register_attendee(
            self.investigator, venue_b["id"], {"person_id": "P-4", "contact_info": "c"}
        )
        self.service.transition(
            self.investigator,
            done["id"],
            "begin_followup",
            {"followup_start": "2026-02-23", "due_at": "2026-03-08"},
        )
        self.service.transition(
            self.investigator, done["id"], "complete_followup", {"outcome": "no symptoms"}
        )

        board = self.service.duty_board()
        owners = {item["owner_id"]: item for item in board["owners"]}
        self.assertEqual(owners["INV-1"]["open_total"], 2)
        self.assertEqual(owners["INV-1"]["venues"][0]["open_count"], 2)
        self.assertEqual(owners["INV-1"]["venues"][0]["name"], "食堂")
        self.assertEqual(
            [item["person_id"] for item in owners["INV-1"]["overdue"]], ["P-3", "P-2"]
        )
        self.assertEqual(owners["INV-2"]["open_total"], 0)
        self.assertEqual(owners["INV-2"]["venues"], [])
        self.assertEqual(owners["INV-2"]["overdue"], [])

        filtered = self.service.duty_board(owner_id="INV-1")
        self.assertEqual([item["owner_id"] for item in filtered["owners"]], ["INV-1"])


if __name__ == "__main__":
    unittest.main()
