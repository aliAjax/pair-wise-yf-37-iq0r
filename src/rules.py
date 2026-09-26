from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

FOLLOWUP_DAYS = 14
VENUE_CASE_STATUSES = ("confirmed", "probable", "recovered")


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _validate_venue(actor, data, lookup):
    case = _find_one(lookup, "case", "id", data.get("case_id"))
    if case is None:
        raise ValidationError("source case not found: " + str(data.get("case_id")))
    if case["status"] not in VENUE_CASE_STATUSES:
        raise ValidationError(
            "venue registration requires a confirmed case, current status: "
            + case["status"]
        )
    try:
        start = _date_ordinal(data.get("exposure_start"))
        end = _date_ordinal(data.get("exposure_end"))
    except (TypeError, ValueError):
        raise ValidationError("exposure_start and exposure_end must be ISO dates")
    if end < start:
        raise ValidationError("exposure_end must not be before exposure_start")


def default_due_at(venue_data):
    end = datetime.fromisoformat(str(venue_data["exposure_end"])[:10]).date()
    return (end + timedelta(days=FOLLOWUP_DAYS)).isoformat()


def is_overdue(due_at, now):
    if not due_at:
        return False
    text = str(due_at)
    try:
        if len(text) <= 10:
            due = datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
            due = due + timedelta(days=1)
        else:
            due = datetime.fromisoformat(text)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return due <= now


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case, 'venue': _validate_venue}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact', 'venues': 'venue'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified', 'venue': 'open'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start'), 'venue': ('case_id', 'name', 'exposure_start', 'exposure_end', 'owner_id')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator'), 'venue': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator')}
    ATTENDEE_ROLES = ('admin', 'investigator')
    ATTENDEE_REQUIRED = ('person_id', 'contact_info')

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

    def validate_attendee(self, actor, venue, data, lookup=None):
        self._ensure_role(actor, self.ATTENDEE_ROLES)
        self._require(data, self.ATTENDEE_REQUIRED)
        if venue["status"] != "open":
            raise InvalidTransition("venue is not open: " + venue["id"])
        case = _find_one(lookup, "case", "id", venue["data"].get("case_id"))
        if case is not None and case["status"] == "closed":
            raise InvalidTransition(
                "source case is closed; no new follow-up items are created"
            )
        return dict(data)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
