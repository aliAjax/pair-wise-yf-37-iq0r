from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, default_due_at, is_overdue


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

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
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def register_attendee(self, actor, venue_id, data=None):
        venue = self.repository.get_entity(venue_id)
        if not venue or venue["kind"] != "venue":
            raise NotFoundError("venue not found: " + str(venue_id))
        payload = self.rules.validate_attendee(actor, venue, dict(data or {}), self._lookup)
        person_id = payload["person_id"]
        existing = None
        for row in self._lookup("contact", "venue_id", venue_id):
            if row["data"].get("person_id") == person_id:
                existing = row
                break
        if existing is not None:
            if existing["status"] == "completed":
                return existing
            merged = dict(existing["data"])
            merged["contact_info"] = payload["contact_info"]
            updated = self.repository.update_entity(
                existing["id"], existing["version"], existing["status"], merged
            )
            self.audit.record(
                existing["id"],
                actor,
                "refresh_attendee",
                existing["status"],
                updated["status"],
                {"patch": {"contact_info": payload["contact_info"]}},
            )
            return updated
        contact_data = {
            "case_id": venue["data"].get("case_id"),
            "venue_id": venue_id,
            "person_id": person_id,
            "contact_info": payload.get("contact_info"),
            "owner_id": venue["data"].get("owner_id"),
            "exposure_start": payload.get("exposure_at") or venue["data"].get("exposure_start"),
            "due_at": payload.get("due_at") or default_due_at(venue["data"]),
        }
        self.rules.validate_create(actor, "contact", contact_data, self._lookup)
        entity = self.repository.create_entity(
            str(uuid4()),
            "contact",
            self.rules.initial_status("contact"),
            contact_data,
            actor.user_id,
        )
        self.audit.record(
            entity["id"],
            actor,
            "create",
            None,
            entity["status"],
            {"kind": "contact", "venue_id": venue_id},
        )
        return entity

    def list_attendees(self, venue_id):
        venue = self.repository.get_entity(venue_id)
        if not venue or venue["kind"] != "venue":
            raise NotFoundError("venue not found: " + str(venue_id))
        return self._lookup("contact", "venue_id", venue_id)

    def duty_board(self, owner_id=None):
        now = datetime.now(timezone.utc)
        venues = {
            item["id"]: item for item in self.repository.list_entities(kind="venue")
        }
        boards = {}
        for contact in self.repository.list_entities(kind="contact"):
            data = contact["data"]
            owner = data.get("owner_id")
            if not owner or (owner_id and owner != owner_id):
                continue
            board = boards.setdefault(
                owner, {"owner_id": owner, "open_total": 0, "venues": {}, "overdue": []}
            )
            if contact["status"] == "completed":
                continue
            board["open_total"] += 1
            venue_id = data.get("venue_id")
            venue = venues.get(venue_id)
            slot = board["venues"].setdefault(
                venue_id or "",
                {
                    "venue_id": venue_id,
                    "name": venue["data"].get("name") if venue else None,
                    "case_id": data.get("case_id"),
                    "open_count": 0,
                },
            )
            slot["open_count"] += 1
            if is_overdue(data.get("due_at"), now):
                board["overdue"].append(
                    {
                        "contact_id": contact["id"],
                        "person_id": data.get("person_id"),
                        "venue_id": venue_id,
                        "venue_name": slot["name"],
                        "status": contact["status"],
                        "due_at": data.get("due_at"),
                    }
                )
        owners = []
        for board in boards.values():
            board["venues"] = sorted(
                board["venues"].values(), key=lambda item: str(item["venue_id"])
            )
            board["overdue"].sort(key=lambda item: str(item["due_at"]))
            owners.append(board)
        owners.sort(key=lambda item: item["owner_id"])
        return {"generated_at": now.isoformat(timespec="seconds"), "owners": owners}

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
