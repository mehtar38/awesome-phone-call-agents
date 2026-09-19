"""
The orchestrator. `run_interpreter_mesh(user, approve=...)` runs the call
sequence that books a clinic appointment together with an ASL interpreter:

    STEP 1  validated user input          (0 calls -- see workflow/user_input.py)
    STEP 2  search & match a clinic       (one at a time, nearest first, insurance-gated)
    STEP 3  line up an interpreter        (own | family list | freelance batches of 3, in parallel)
    STEP 4  ask the user                  (nothing is booked until they say yes)
    STEP 5  book, confirm, notify         (clinic first, then a freelance
                                           interpreter's binding confirm, then texts)

Clinics are FOUND, not supplied: clinic_lookup.find_clinics() turns the user's
ZIP and appointment type into up to 10 nearby candidates. Freelance
interpreters come from the seeded state roster within travel distance of the
matched clinic. Both can still be injected for tests.

Ordering is what keeps the fee-bearing commitment last. Nothing binding
happens before the user approves; after that the clinic is booked first
(a patient booking is normally free to cancel), and only then is a freelance
interpreter asked to commit. If every interpreter declines, the clinic
booking is cancelled.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..calle.run import reset_call_routing

from . import calendar, clinic_call, clinic_lookup, family_call, interpreter_matching, reminders
from .confirmation import ApprovalFn, BookingDeclined, BookingProposal
from .types import (
    AppointmentResult,
    ClinicCandidate,
    ClinicSlot,
    FamilyInterpreter,
    InterpreterCandidate,
    UserInput,
    append_note,
    clean_strings,
)

INTERPRETER_RADIUS_MILES = 15.0


@dataclass
class _Arrangement:
    """The interpreter side of the plan, settled before anything is booked."""

    slot: ClinicSlot
    source: str  # "user_arranged" | "family" | "freelance"
    family_tier_result: str
    evidence: list[str]
    family_member: FamilyInterpreter | None = None
    # Freelance only: the cheapest match first, then anyone else who named this
    # same slot -- the fallbacks if the first declines the final confirmation.
    freelancers: list[InterpreterCandidate] = field(default_factory=list)

    def person(self) -> "FamilyInterpreter | InterpreterCandidate | None":
        """Who the agent will contact for the interpreter, if anyone."""
        if self.family_member is not None:
            return self.family_member
        return self.freelancers[0] if self.freelancers else None


def run_interpreter_mesh(
    user: UserInput,
    clinics: list[ClinicCandidate] | None = None,
    candidates: list[InterpreterCandidate] | None = None,
    *,
    approve: ApprovalFn,
) -> AppointmentResult:
    """
    `approve` is asked once, with a BookingProposal, after a clinic and an
    interpreter are lined up. Nothing is booked unless it returns True; if it
    returns False (or raises BookingDeclined) the run ends with BookingDeclined.

    `clinics` and `candidates` default to None, which means "go find them"
    (clinic_lookup.find_clinics / interpreter_matching.load_candidates_within_radius).
    Tests pass explicit lists instead so no network or roster lookup is
    involved.
    """

    # Real-mode calls are routed onto the three owned demo lines, and those
    # assignments must not leak between runs in the same process.
    reset_call_routing()

    # --- STEP 2: search & match a clinic ----------------------------------
    if clinics is None:
        clinics = clinic_lookup.find_clinics(user.zipcode, user.appointment_type)
    if not clinics:
        raise RuntimeError(
            f"No {user.appointment_type.value} clinic could be found near "
            f"{user.zipcode} -- nothing to call."
        )
    clinic, matched = clinic_call.search_clinics(clinics, user)
    if clinic is None:
        raise RuntimeError(
            "No clinic in the search list both accepts insurance and has a "
            "slot the user can attend -- nothing to book."
        )

    # --- STEP 3: line up an interpreter -----------------------------------
    arrangement = _arrange_interpreter(user, clinic, matched, candidates)

    # --- STEP 4: ask the user ---------------------------------------------
    if not approve(_build_proposal(clinic, arrangement)):
        raise BookingDeclined("The user declined the proposed appointment.")

    # --- STEP 5: book, confirm, notify ------------------------------------
    return _book_and_confirm(user, clinic, arrangement)


def describe_goal(user: UserInput) -> str:
    """What the user is told will happen, and what will be shared. It has to
    state the search breadth (up to 10 clinics, then family, then batches of
    interpreters, 3 at a time) and exactly which personal details each kind of call gets,
    because the user's submission is their consent."""
    provider = user.insurance.provider_name
    text = (
        f"Book a {user.appointment_type.value.replace('_', ' ')} appointment "
        f"near {user.zipcode} and secure an ASL interpreter for it. To do "
        f"that I'll phone nearby clinics (up to 10, one at a time, nearest "
        f"first, stopping at the first that accepts your insurance and has a "
        f"time you're free). On those calls I'll say the "
        f"patient is deaf or hard of hearing, share your insurance provider "
        f"({provider}) if asked, and share the times you're free "
        f"({calendar.describe_windows(user.free_windows)}). "
    )
    if not user.has_interpreter:
        family_step = "your family list one at a time, then " if user.family else ""
        family_told = " Family members are also told your name." if user.family else ""
        text += (
            f"Then I'll phone {family_step}freelance interpreters 3 at a time, "
            f"telling them the matched appointment times.{family_told} "
        )
    text += (
        f"Before I book anything I'll show you the clinic, the time and the "
        f"interpreter and ask you to approve; if you say no, nothing is "
        f"booked. If you say yes, I'll call the clinic back to book"
        f"{'' if user.has_interpreter else ', then confirm the interpreter'}. "
        f"On that booking call only, I'll give the clinic your name, date of "
        f"birth, age, phone number and insurance details ({provider}, policy "
        f"{user.insurance.policy_number}) so they can put the appointment in "
        f"their book. If a clinic says something is required for the "
        f"appointment, such as a referral, I'll tell you before you decide."
    )
    return text


# --- Step 3 ----------------------------------------------------------------

def _arrange_interpreter(
    user: UserInput,
    clinic: ClinicCandidate,
    matched: list[ClinicSlot],
    candidates: list[InterpreterCandidate] | None,
) -> _Arrangement:
    """Settles who will interpret, and for which of the matched slots, without
    committing anyone."""
    if user.has_interpreter:
        # No contact details are collected for that interpreter, so nobody is
        # called or texted for them. Their availability isn't collected either,
        # so where several slots matched, one is picked at random.
        return _Arrangement(
            slot=random.choice(matched),
            source="user_arranged",
            family_tier_result="not_applicable",
            evidence=[
                f"User arranged their own interpreter; no contact details on "
                f"file, so none was called or texted. Slot chosen at random "
                f"from {len(matched)} matched slot(s)."
            ],
        )

    if user.family:
        member, slot = family_call.call_family_in_order(user.family, matched, user)
        if member is not None:
            return _Arrangement(
                slot=slot,
                source="family",
                family_tier_result="locked_in",
                family_member=member,
                evidence=[
                    f"Family member {member.name} ({member.relation}) locked "
                    f"in for {slot.date} {slot.time} (list order, stopped there)."
                ],
            )

    return _arrange_freelancer(user, clinic, matched, candidates)


def _arrange_freelancer(
    user: UserInput,
    clinic: ClinicCandidate,
    matched: list[ClinicSlot],
    candidates: list[InterpreterCandidate] | None,
) -> _Arrangement:
    """The batch search stops at the first batch with a match. Its cheapest
    match is the one proposed; the rest of that batch who named the same slot
    are kept as fallbacks for the final confirmation."""
    if candidates is None:
        candidates = interpreter_matching.load_candidates_within_radius(
            clinic.zipcode, INTERPRETER_RADIUS_MILES
        )
    if not candidates:
        raise RuntimeError(
            f"No interpreter in the roster is within {INTERPRETER_RADIUS_MILES} "
            f"miles of {clinic.name} ({clinic.zipcode}) -- nobody was called, "
            f"and no clinic appointment was taken."
        )

    ranked = interpreter_matching.search_freelancers_in_batches(candidates, matched)
    if not ranked:
        raise RuntimeError(
            "No freelance interpreter in any batch can cover a matched slot "
            "-- nothing booked, and no clinic appointment was taken."
        )

    slot = interpreter_matching.slot_for_candidate(ranked[0], matched)
    if slot is None:
        raise RuntimeError(
            f"{ranked[0].name} matched the batch but named none of the matched "
            f"slots -- refusing to guess a time."
        )
    fallbacks = [c for c in ranked[1:] if slot.key() in c.coverable_slots]
    return _Arrangement(
        slot=slot,
        source="freelance",
        family_tier_result="no_overlap" if user.family else "not_registered",
        evidence=[],
        freelancers=[ranked[0], *fallbacks],
    )


# --- Step 4 ----------------------------------------------------------------

def _freelancer_detail(candidate: InterpreterCandidate) -> dict:
    return {
        "tier": "freelance",
        "name": candidate.name,
        "rate_per_hour": candidate.rate_per_hour,
        "minimum_hours": candidate.minimum_hours,
        "total_estimate": candidate.total_cost(),
        "expertise": candidate.expertise,
    }


def _interpreter_detail(arrangement: _Arrangement) -> dict:
    if arrangement.source == "user_arranged":
        return {"tier": "user_arranged"}
    if arrangement.source == "family":
        member = arrangement.family_member
        return {"tier": "family", "name": member.name, "relation": member.relation}
    return _freelancer_detail(arrangement.freelancers[0])


def _notes(clinic: ClinicCandidate, person, booking: dict | None = None) -> list[str]:
    """Concerns raised on the calls so far, each prefixed with who said it."""
    entries = [(clinic.name, clinic.notes)]
    if booking is not None:
        entries.append(
            (f"{clinic.name} (booking call)", append_note(None, booking.get("additional_notes")))
        )
    if person is not None:
        entries.append((person.name, person.notes))
    return [f"{who}: {text}" for who, text in entries if text]


def _build_proposal(clinic: ClinicCandidate, arrangement: _Arrangement) -> BookingProposal:
    return BookingProposal(
        clinic_name=clinic.name,
        clinic_zipcode=clinic.zipcode,
        clinic_distance_miles=clinic.distance_miles,
        date=arrangement.slot.date,
        time=arrangement.slot.time,
        interpreter=_interpreter_detail(arrangement),
        alternates=[_freelancer_detail(c) for c in arrangement.freelancers[1:]],
        requirements=list(clinic.requirements),
        notes=_notes(clinic, arrangement.person()),
    )


# --- Step 5 ----------------------------------------------------------------

def _book_and_confirm(
    user: UserInput, clinic: ClinicCandidate, arrangement: _Arrangement
) -> AppointmentResult:
    """Books the clinic -- giving them the patient's details, which no earlier
    call did -- then has a freelance interpreter confirm, then texts the user
    and whoever was secured.

    The booking call raises if it didn't actually book (see
    clinic_call._require_booked), and nothing fee-bearing exists yet at that
    point.

    NOT implemented: the design's "add the appointment to the user's
    calendar" step. No mechanism is specified for it and none is invented
    here; see README.
    """
    slot = arrangement.slot
    booking = clinic_call.book_slot(clinic, slot, user)

    if arrangement.source == "freelance":
        interpreter = _confirm_freelancer(user, clinic, slot, booking, arrangement.freelancers)
        interpreter_detail = {**_freelancer_detail(interpreter), "confirmed": True}
        arrangement.evidence.append(
            f"{interpreter.name} confirmed for {slot.date} {slot.time} at "
            f"${interpreter.rate_per_hour}/hr."
        )
    else:
        interpreter = arrangement.family_member  # None when the user arranged their own
        interpreter_detail = _interpreter_detail(arrangement)

    distance = (
        f"{clinic.distance_miles} mi" if clinic.distance_miles is not None else "distance unknown"
    )
    evidence = [
        f"Clinic {clinic.name} [{clinic.clinic_type}] ({clinic.zipcode}, "
        f"{distance}, source={clinic.source}) matched and booked for "
        f"{slot.date} {slot.time}.",
        *arrangement.evidence,
    ]
    requirements = list(
        dict.fromkeys([*clinic.requirements, *clean_strings(booking.get("requirements"))])
    )
    evidence.extend(_send_confirmations(user, clinic, slot, interpreter, requirements))
    return AppointmentResult(
        appointment=booking,
        interpreter=interpreter_detail,
        family_tier_result=arrangement.family_tier_result,
        interpreter_source=arrangement.source,
        cancellation_deadline=None,  # TODO: derive from the interpreter's own
                                      # policy, once real interpreter data exists
        evidence=evidence,
        requirements=requirements,
        notes=_notes(clinic, interpreter, booking),
    )


def _confirm_freelancer(
    user: UserInput,
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    booking: dict,
    freelancers: list[InterpreterCandidate],
) -> InterpreterCandidate:
    """The one binding ask, made after the clinic is booked. The first
    freelancer to say yes gets the engagement. If none does, the clinic
    booking is cancelled so no appointment is left standing without an
    interpreter."""
    for candidate in freelancers:
        if interpreter_matching.confirm_interpreter(candidate, slot):
            return candidate

    reference = booking.get("booking_reference")
    if clinic_call.cancel_booking(clinic, slot, user, reference):
        outcome = f"The booking at {clinic.name} was cancelled."
    else:
        outcome = (
            f"The booking at {clinic.name} ({clinic.phone}) could NOT be "
            f"cancelled automatically -- please call them to cancel it "
            f"(reference: {reference or 'none given'})."
        )
    raise RuntimeError(
        f"Every interpreter who could cover {slot.date} {slot.time} declined "
        f"the final confirmation. {outcome}"
    )


def _send_confirmations(
    user: UserInput,
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    interpreter: "FamilyInterpreter | InterpreterCandidate | None",
    requirements: list[str],
) -> list[str]:
    """Both confirmation texts -- or just the user's, when the user brought
    their own interpreter and the agent has no way to reach them. No SMS
    provider is wired up (the design names none), so each send degrades to an
    evidence line instead of failing a run whose appointment is genuinely
    booked."""
    interpreter_name = interpreter.name if interpreter else None
    recipients = [("user", user.phone_number, "You")]
    if interpreter is not None:
        recipients.append(("interpreter", interpreter.phone, interpreter.name))

    lines = []
    for label, phone, recipient_name in recipients:
        body = reminders.confirmation_text_body(
            recipient_name, clinic.name, slot, interpreter_name,
            requirements=requirements if label == "user" else None,
        )
        try:
            reminders.send_confirmation_text(phone, body)
        except NotImplementedError:
            lines.append(
                f"Confirmation text to {label} ({phone}) NOT sent: no "
                f"SMS provider wired up. Message was: {body}"
            )
        else:
            lines.append(f"Confirmation text sent to {label} ({phone}).")
    if interpreter is None:
        lines.append(
            "No interpreter notification: the user arranged their own "
            "interpreter and no contact details were collected for them."
        )
    return lines
