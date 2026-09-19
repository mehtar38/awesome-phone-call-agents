"""
Shared types for the Interpreter Mesh workflow.

`UserInput` is what the whole workflow runs on. It is built by
workflow/user_input.py from a JSON object validated against
schemas/user_input.schema.json -- that schema, not this file, is the
authoritative definition of the input contract. How the JSON gets produced
(typed in, signed and recognized, or read back from a registered profile) is
outside both.

Replaces the older `Intent`, which carried urgency, a free-form need type, a
visit-sensitivity flag driving an appropriateness gate, a search window in
days, and the raw gloss text. None of those exist anymore.
"""

from __future__ import annotations


from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AppointmentType(str, Enum):
    """The clinic categories the user can ask for. Each maps to a search
    phrase in workflow/clinic_lookup.py."""

    PHYSICAL_THERAPY = "physical_therapy"
    EYES = "eyes"
    ENT = "ent"
    DENTAL = "dental"
    MENTAL = "mental"
    GENERAL = "general"


@dataclass
class TimeWindow:
    start: datetime
    end: datetime

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "TimeWindow") -> "TimeWindow | None":
        """Currently unreferenced. Its only caller was a SHORTCUT-path
        constraint calculation that no longer exists: a user who already has
        an interpreter supplies no interpreter availability to intersect
        with, so clinic slots are matched against the user's own windows on
        every path."""
        if not self.overlaps(other):
            return None
        return TimeWindow(max(self.start, other.start), min(self.end, other.end))


@dataclass
class Insurance:
    provider_name: str
    policy_number: str


@dataclass
class FamilyInterpreter:
    """One pre-registered family member who signs. Reached by phone ONLY,
    one at a time in list order, and only after a clinic and its matched
    slots already exist -- each is asked which of those slots they can
    cover. Once the appointment is booked they get a confirmation text,
    same as the user."""

    name: str
    relation: str
    phone: str
    # Their own answer to "which of these specific slots can you cover", as
    # "YYYY-MM-DD HH:MM" strings -- same contract as
    # InterpreterCandidate.coverable_slots below.
    coverable_slots: list[str] = field(default_factory=list)
    notes: str | None = None  # concerns they raised on the call, if any


@dataclass
class UserInput:
    """The parsed, validated user input. See schemas/user_input.schema.json."""

    name: str
    date_of_birth: str                # YYYY-MM-DD, as given
    age: int                          # as given; deliberately NOT re-derived
                                       # from date_of_birth
    phone_number: str                 # US E.164; where the confirmation text goes
    zipcode: str                      # 5-digit US ZIP; the clinic search origin
    appointment_type: AppointmentType
    insurance: Insurance
    # One entry per "friday 2PM"-style slot, already expanded to the hour it
    # covers (2PM -> 14:00-15:00).
    free_windows: list[TimeWindow]
    has_interpreter: bool             # True: the user arranged their own. No
                                       # contact details are collected for that
                                       # person, so nothing calls or texts them.
    family: list[FamilyInterpreter] = field(default_factory=list)

    def patient_summary(self) -> str:
        """What the booking call is allowed to give the clinic. The clinic
        SEARCH leg never says any of this -- it only asks whether the clinic
        accepts insurance as a matter of policy."""
        return (
            f"patient name {self.name}, date of birth {self.date_of_birth}, "
            f"age {self.age}, phone {self.phone_number}, insurance "
            f"{self.insurance.provider_name}, policy number "
            f"{self.insurance.policy_number}"
        )


@dataclass
class ClinicCandidate:
    """One clinic found by workflow/clinic_lookup.py, before and after the
    Step 2 search calls it.

    The first four fields are the whole clinic record -- see
    schemas/clinic_record.schema.json. `clinic_type` is the Google Maps
    listing's own category ("Dentist"), which asserts something real about
    the business; only when Maps returns no category does it fall back to the
    label that was searched for. The rest is runtime state, not part of the
    record: `distance_miles` is None when the clinic's ZIP isn't in the
    centroid table, and every sort here must handle that.
    """

    name: str
    phone: str
    zipcode: str
    clinic_type: str
    distance_miles: float | None = None
    source: str = "apify_google_maps"  # "apify_google_maps" | "fallback_snapshot" | "injected"
    accepts_insurance: bool | None = None
    offered_slots: list[ClinicSlot] = field(default_factory=list)
    # What the clinic said is needed to book or attend (a referral, ID, forms).
    # Informational: it never changes which clinic is chosen.
    requirements: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass
class InterpreterCandidate:
    """One freelance interpreter from the seeded roster, before and after the
    batched search contacts them."""

    name: str
    phone: str
    zipcode: str = ""
    city: str = ""
    age: int | None = None
    expertise: list[str] = field(default_factory=list)  # stored and surfaced;
                                                          # never used to filter
    distance_miles: float | None = None
    # Populated only after they actually respond -- never assumed upfront, and
    # never stored in the roster, since real rates aren't public.
    rate_per_hour: float | None = None
    minimum_hours: float | None = None
    can_make_target_slot: bool | None = None
    # The interpreter's own answer to "which of these slots work for you" --
    # e.g. "2026-09-24 14:00". Fixed a real bug (confirmed by an actual test
    # run, not just inspection): this used to be read as a truthiness check
    # only and then discarded, so a candidate who could ONLY cover Monday
    # was booked for Thursday because nothing downstream ever checked which
    # slot she'd actually agreed to.
    coverable_slots: list[str] = field(default_factory=list)
    notes: str | None = None  # concerns they raised on the call, if any

    def total_cost(self) -> float | None:
        return (
            None
            if self.rate_per_hour is None or self.minimum_hours is None
            else self.rate_per_hour * self.minimum_hours
        )


@dataclass
class ClinicSlot:
    date: str   # ISO date, e.g. "2026-09-24"
    time: str   # "14:00"

    def key(self) -> str:
        """The exact string form every other party is asked about and
        answers with. Family and freelance matching are both exact-string
        membership tests against this -- see README's known limitations for
        what that costs if a real call answers "Thu 2pm" instead."""
        return f"{self.date} {self.time}"


@dataclass
class AppointmentResult:
    """Matches the JSON shape in CALL-E Hackathon Ideas.md's Use Case."""

    appointment: dict
    interpreter: dict | None
    family_tier_result: str  # "locked_in" | "no_overlap" | "not_registered"
    interpreter_source: str  # "family" | "freelance" | "user_arranged"
    cancellation_deadline: str | None
    evidence: list[str] = field(default_factory=list)
    # Things the clinic said are required for the appointment (a referral,
    # ID, forms) -- for the user to act on. Never blocks a booking by itself.
    requirements: list[str] = field(default_factory=list)
    # Other concerns raised on any call, each prefixed with who said it.
    notes: list[str] = field(default_factory=list)


def clean_strings(raw: object) -> list[str]:
    """The non-blank strings in a list a call returned, ignoring anything
    malformed."""
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def append_note(current: "str | None", new: object) -> "str | None":
    """Adds a note a call returned, ignoring blanks and non-strings."""
    if not isinstance(new, str) or not new.strip():
        return current
    return f"{current}; {new.strip()}" if current else new.strip()


def distance_sort_key(distance_miles: float | None) -> tuple[bool, float]:
    """Sorts unknown distances last instead of raising. Every distance in
    this codebase can be None (a ZIP outside the centroid table), and a bare
    `key=lambda c: c.distance_miles` raises TypeError the moment one is."""
    return (distance_miles is None, distance_miles if distance_miles is not None else 0.0)
