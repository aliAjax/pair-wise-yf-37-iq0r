from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import RuleEngine

OPEN_CONTACT_STATUSES = ("identified", "following")


class DomainService:
    def __init__(self, repository, rules=None, clock=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if (entity["kind"], action) == ("case", "register_exposure"):
            return self._register_exposure(actor, entity, expected, patch)
        if (entity["kind"], action) == ("exposure", "update_roster"):
            return self._update_roster(actor, entity, expected, patch)
        return self._apply(actor, entity, expected, next_status, patch, action)

    def _apply(self, actor, entity, expected_version, status, patch, action):
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected_version, status, merged)
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    @staticmethod
    def _build_attendees(contacts):
        """Merge repeated rows for one person inside a single roster.

        Earliest exposure moment is kept; later contact details win.
        """
        attendees = {}
        order = []
        for entry in contacts:
            person_id = entry["person_id"]
            if person_id not in attendees:
                attendees[person_id] = dict(entry)
                attendees[person_id]["attendance_count"] = 1
                order.append(person_id)
                continue
            current = attendees[person_id]
            current["attendance_count"] += 1
            if str(entry.get("exposure_start", "")) < str(current.get("exposure_start", "")):
                current["exposure_start"] = entry.get("exposure_start")
            for key in ("phone", "contact_detail"):
                if entry.get(key):
                    current[key] = entry[key]
        return [attendees[person_id] for person_id in order]

    def _register_exposure(self, actor, case, expected_version, patch):
        venue = patch["venue"]
        attendees = self._build_attendees(patch["contacts"])
        exposure_data = {
            "case_id": case["id"],
            "venue": venue,
            "window_start": patch["window_start"],
            "window_end": patch["window_end"],
            "owner": patch.get("owner") or actor.user_id,
            "due_at": patch.get("due_at"),
            "registered_by": patch["registered_by"],
            "contacts": attendees,
        }
        exposure = self.repository.create_entity(
            str(uuid4()), "exposure", "registered", exposure_data, actor.user_id
        )
        result = self._sync_followups(actor, case, exposure, attendees)
        exposure_data["followups"] = result
        exposure = self.repository.update_entity(
            exposure["id"], exposure["version"], "registered", exposure_data
        )
        self.audit.record(
            exposure["id"], actor, "create", None, "registered",
            {"kind": "exposure", "case_id": case["id"], "venue": venue},
        )
        case_data = dict(case["data"])
        case_data.setdefault("exposure_ids", []).append(exposure["id"])
        updated_case = self.repository.update_entity(
            case["id"], expected_version, case["status"], case_data
        )
        self.audit.record(
            case["id"], actor, "register_exposure", case["status"], case["status"],
            {"exposure_id": exposure["id"], "venue": venue,
             "created": result["created"], "updated": len(result["updated"]),
             "completed_skipped": result["completed_skipped"]},
        )
        return updated_case

    def _update_roster(self, actor, exposure, expected_version, patch):
        case = self.repository.get_entity(exposure["data"]["case_id"])
        if case and case["status"] == "closed":
            raise InvalidTransition("case is closed; roster updates are not allowed")
        data = dict(exposure["data"])
        data["contacts"] = self._build_attendees(patch["contacts"])
        result = self._sync_followups(
            actor, case, {**exposure, "data": data}, data["contacts"]
        )
        data["followups"] = result
        updated = self.repository.update_entity(
            exposure["id"], expected_version, "registered", data
        )
        self.audit.record(
            exposure["id"], actor, "update_roster", "registered", "registered",
            {"created": result["created"], "updated": len(result["updated"]),
             "completed_skipped": result["completed_skipped"]},
        )
        return updated

    def _sync_followups(self, actor, case, exposure, attendees):
        """Create or update per-person followup items for one exposure event.

        - new person: a contact followup is created, linked to source case
        - open followup for the same person: refresh contact info, link the
          activity; first exposure time and owner are preserved
        - completed followup: nothing is generated again
        """
        case_id = exposure["data"]["case_id"]
        contacts = [
            item
            for item in self._lookup("contact", "case_id", case_id)
            if item["status"] in OPEN_CONTACT_STATUSES
        ]
        by_person = {item["data"].get("person_id"): item for item in contacts}
        created = 0
        updated = []
        completed_skipped = 0
        for attendee in attendees:
            person_id = attendee["person_id"]
            existing = by_person.get(person_id)
            if existing is None:
                all_items = self._lookup("contact", "case_id", case_id)
                finished = [
                    item for item in all_items
                    if item["data"].get("person_id") == person_id
                    and item["status"] not in OPEN_CONTACT_STATUSES
                ]
                if finished:
                    completed_skipped += 1
                    continue
                payload = {
                    "case_id": case_id,
                    "person_id": person_id,
                    "exposure_start": attendee.get("exposure_start")
                    or exposure["data"]["window_start"],
                    "exposure_ids": [exposure["id"]],
                    "venue": exposure["data"]["venue"],
                    "owner": exposure["data"]["owner"],
                    "due_at": exposure["data"].get("due_at"),
                }
                if attendee.get("phone"):
                    payload["phone"] = attendee["phone"]
                if attendee.get("contact_detail"):
                    payload["contact_detail"] = attendee["contact_detail"]
                contact = self.repository.create_entity(
                    str(uuid4()), "contact", "identified", payload, actor.user_id
                )
                self.audit.record(
                    contact["id"], actor, "create", None, "identified",
                    {"kind": "contact", "case_id": case_id,
                     "exposure_id": exposure["id"]},
                )
                created += 1
                continue
            data = dict(existing["data"])
            for key in ("phone", "contact_detail"):
                if attendee.get(key):
                    data[key] = attendee[key]
            data.setdefault("owner", exposure["data"]["owner"])
            if not data.get("due_at") and exposure["data"].get("due_at"):
                data["due_at"] = exposure["data"]["due_at"]
            data["venue"] = exposure["data"]["venue"]
            links = data.setdefault("exposure_ids", [])
            if exposure["id"] not in links:
                links.append(exposure["id"])
            refreshed = self.repository.update_entity(
                existing["id"], existing["version"], existing["status"], data
            )
            self.audit.record(
                existing["id"], actor, "refresh_contact",
                existing["status"], existing["status"],
                {"exposure_id": exposure["id"]},
            )
            updated.append(refreshed["id"])
        return {"created": created, "updated": updated,
                "completed_skipped": completed_skipped}

    def duty_board(self, owner=None, as_of=None):
        now = self.clock() if as_of is None else self._parse_duty_moment(as_of)
        exposures = {
            item["id"]: item for item in self.repository.list_entities(kind="exposure")
        }
        cases = {item["id"]: item for item in self.repository.list_entities(kind="case")}
        venues = {}
        overdue = []
        for contact in self.repository.list_entities(kind="contact"):
            if contact["status"] not in OPEN_CONTACT_STATUSES:
                continue
            data = contact["data"]
            if owner is not None and data.get("owner") != owner:
                continue
            due_at = data.get("due_at")
            is_overdue = bool(due_at and self._parse_duty_moment(due_at) < now)
            linked = []
            for exposure_id in data.get("exposure_ids", []):
                exposure = exposures.get(exposure_id)
                if exposure:
                    linked.append(exposure["data"]["venue"])
            if data.get("venue") and not linked:
                linked.append(data["venue"])
            counted_venues = set()
            for venue_name in linked:
                if venue_name in counted_venues:
                    continue
                counted_venues.add(venue_name)
                venue = venues.setdefault(
                    venue_name, {"venue": venue_name, "open_count": 0, "overdue": []}
                )
                venue["open_count"] += 1
                if is_overdue:
                    case = cases.get(data.get("case_id"))
                    venue["overdue"].append({
                        "contact_id": contact["id"],
                        "person_id": data.get("person_id"),
                        "owner": data.get("owner"),
                        "due_at": due_at,
                        "case_id": data.get("case_id"),
                        "case_closed": bool(case and case["status"] == "closed"),
                    })
            if is_overdue:
                case = cases.get(data.get("case_id"))
                overdue.append({
                    "contact_id": contact["id"],
                    "person_id": data.get("person_id"),
                    "owner": data.get("owner"),
                    "due_at": due_at,
                    "venue": linked[0] if linked else data.get("venue"),
                    "case_id": data.get("case_id"),
                    "case_closed": bool(case and case["status"] == "closed"),
                })
        return {
            "as_of": now.isoformat(timespec="seconds"),
            "owner": owner,
            "venues": sorted(venues.values(), key=lambda item: item["venue"]),
            "overdue": sorted(overdue, key=lambda item: str(item["due_at"])),
        }

    @staticmethod
    def _parse_duty_moment(value):
        if isinstance(value, datetime):
            return value
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

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
