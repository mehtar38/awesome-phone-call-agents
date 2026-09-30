"""
The orchestrator. `run_interpreter_mesh(user, approve=...)` runs the call
sequence that books a clinic appointment together with an ASL interpreter:

    STEP 1  validated user input          (0 calls -- see workflow/user_input.py)
    STEP 2  search & match a clinic       (one at a time, nearest first, insurance-gated)
    STEP 3  line up an interpreter        (own | family list | freelance, batches of interpreter_matching.DEFAULT_BATCH_SIZE -- dialled one after another unless PARALLEL_BATCH_CALLS is on, see that module's docstring)
    STEP 4  ask the user                  (nothing is booked until they say yes)
    STEP 5  book, confirm, notify         (clinic first, then a freelance
                                           interpreter's binding confirm, then texts)

Clinics are FOUND, not supplied: clinic_lookup.find_clinics() turns the user's
ZIP and appointment type into up to 10 nearby candidates. Freelance
interpreters come from the seeded state roster within travel distance of the
matched clinic. Both can still be injected for tests.

Ordering is what keeps the fee-bearing commitment last. Nothing binding
happens before the user approves; after that the clinic is booked first
(a patient booking is normally free to cancel), and only then is the
interpreter -- family or freelance -- asked to commit for real. A family
member's "yes" during Step 3A was given to a slot that wasn't booked yet, so
they get the same binding confirm call a freelancer does; if they decline, the
next unreached family member is tried, then freelance interpreters for that
one slot. If nobody confirms, the clinic booking is cancelled.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import NoReturn

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
    # Family only: whoever is left on the list after family_member, in order
    # -- never asked during the search (it stopped at the first yes), so
    # they're available as a fallback if family_member declines the binding
    # confirm.
    remaining_family: list[FamilyInterpreter] = field(default_factory=list)
    # Freelance only: the cheapest match first, then anyone else who named this
    # same slot -- the fallbacks if the first declines the final confirmation.
    freelancers: list[InterpreterCandidate] = field(default_factory=list)
    # Family only: the caller-supplied candidate pool (tests) or None ("go
    # find them"), carried unresolved so the family-confirm fallback can
    # search the SAME pool a freelance-first run would have -- never the real
    # roster when a test injected a fake one.
    freelance_pool: list[InterpreterCandidate] | None = None

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
    interpreters, interpreter_matching.DEFAULT_BATCH_SIZE at a time) and
    exactly which personal details each kind of call gets, because the
    user's submission is their consent. The batch size is read from that
    same constant, never restated as a literal, so this text can't drift
    from what search_freelancers_in_batches() actually does."""
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
        batch_size = interpreter_matching.DEFAULT_BATCH_SIZE
        batch_phrase = "one at a time" if batch_size == 1 else f"{batch_size} at a time"
        text += (
            f"Then I'll phone {family_step}freelance interpreters "
            f"{batch_phrase}, telling them the matched appointment times."
            f"{family_told} "
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
        member, slot = family_call.call_family_in_order(user.family, matched, user, clinic)
        if member is not None:
            return _Arrangement(
                slot=slot,
                source="family",
                family_tier_result="locked_in",
                family_member=member,
                # Identity, not equality: two entries could otherwise share
                # every field and be mistaken for each other.
                remaining_family=user.family[
                    next(i for i, m in enumerate(user.family) if m is member) + 1 :
                ],
                freelance_pool=candidates,
                evidence=[
                    f"Family member {member.name} ({member.relation}) locked "
                    f"in for {slot.date} {slot.time} (list order, stopped there)."
                ],
            )

    return _arrange_freelancer(user, clinic, matched, candidates)


def _resolve_freelance_pool(
    clinic: ClinicCandidate, candidates: list[InterpreterCandidate] | None
) -> list[InterpreterCandidate]:
    """`candidates` is the caller-supplied pool (tests inject one), or None,
    meaning "go find them" in the seeded roster. Shared by the initial
    freelance search and the family-confirm fallback so both ever look at
    the same pool for a given run."""
    if candidates is None:
        candidates = interpreter_matching.load_candidates_within_radius(
            clinic.zipcode, INTERPRETER_RADIUS_MILES
        )
    return candidates


def _arrange_freelancer(
    user: UserInput,
    clinic: ClinicCandidate,
    matched: list[ClinicSlot],
    candidates: list[InterpreterCandidate] | None,
) -> _Arrangement:
    """The batch search stops at the first batch with a match. Its cheapest
    match is the one proposed; the rest of that batch who named the same slot
    are kept as fallbacks for the final confirmation."""
    candidates = _resolve_freelance_pool(clinic, candidates)
    if not candidates:
        raise RuntimeError(
            f"No interpreter in the roster is within {INTERPRETER_RADIUS_MILES} "
            f"miles of {clinic.name} ({clinic.zipcode}) -- nobody was called, "
            f"and no clinic appointment was taken."
        )

    ranked = interpreter_matching.search_freelancers_in_batches(
        candidates, matched, clinic, batch_size=interpreter_matching.DEFAULT_BATCH_SIZE
    )
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
        "phone": candidate.phone,  # needed later if the user cancels -- see cancel_appointment()
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
        return {
            "tier": "family", "name": member.name, "relation": member.relation,
            "phone": member.phone,
        }
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
    call did -- then has the interpreter confirm for real, then texts the
    user and whoever was secured.

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
        source = "freelance"
    elif arrangement.source == "family":
        interpreter, source = _confirm_family(user, clinic, slot, booking, arrangement)
        if source == "family":
            interpreter_detail = {
                "tier": "family", "name": interpreter.name,
                "relation": interpreter.relation, "phone": interpreter.phone,
                "confirmed": True,
            }
            arrangement.evidence.append(
                f"{interpreter.name} ({interpreter.relation}) confirmed for "
                f"{slot.date} {slot.time}."
            )
        else:
            # Every family member declined the binding confirm; a freelance
            # interpreter covered this one already-booked slot instead.
            interpreter_detail = {**_freelancer_detail(interpreter), "confirmed": True}
            arrangement.evidence.append(
                f"No family member could confirm {slot.date} {slot.time}; "
                f"{interpreter.name} (freelance) confirmed instead at "
                f"${interpreter.rate_per_hour}/hr."
            )
    else:
        interpreter = arrangement.family_member  # None when the user arranged their own
        interpreter_detail = _interpreter_detail(arrangement)
        source = arrangement.source

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
        interpreter_source=source,
        cancellation_deadline=None,  # TODO: derive from the interpreter's own
                                      # policy, once real interpreter data exists
        clinic_contact={
            "name": clinic.name, "phone": clinic.phone,
            "zipcode": clinic.zipcode, "address": clinic.address,
        },
        appointment_slot={"date": slot.date, "time": slot.time},
        evidence=evidence,
        requirements=requirements,
        notes=_notes(clinic, interpreter, booking),
    )


def _cancel_and_raise(
    user: UserInput, clinic: ClinicCandidate, slot: ClinicSlot, booking: dict, reason: str
) -> NoReturn:
    """Shared by both confirm paths below: nobody confirmed, so the booking
    that was made on the strength of an earlier non-binding "yes" has to be
    undone. If the clinic won't cancel, the user has to be told which
    booking to undo themselves."""
    reference = booking.get("booking_reference")
    if clinic_call.cancel_booking(clinic, slot, user.name, reference):
        outcome = f"The booking at {clinic.name} was cancelled."
    else:
        outcome = (
            f"The booking at {clinic.name} ({clinic.phone}) could NOT be "
            f"cancelled automatically -- please call them to cancel it "
            f"(reference: {reference or 'none given'})."
        )
    raise RuntimeError(f"{reason} {outcome}")


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
        if interpreter_matching.confirm_interpreter(candidate, slot, clinic, user):
            return candidate
    _cancel_and_raise(
        user, clinic, slot, booking,
        f"Every interpreter who could cover {slot.date} {slot.time} declined "
        f"the final confirmation.",
    )


def _confirm_family(
    user: UserInput,
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    booking: dict,
    arrangement: _Arrangement,
) -> "tuple[FamilyInterpreter | InterpreterCandidate, str]":
    """The binding ask for a family arrangement, made after the clinic is
    booked. A family member's "yes" during Step 3A was given to a slot that
    wasn't booked yet, so the locked-in member is called back to confirm for
    real. If they decline, the next unreached family member is asked the same
    question directly -- the search stopped at the first yes, so nobody after
    that member was ever contacted. If the whole family list declines, the
    search falls through to freelance interpreters for this one already-
    booked slot, exactly as it would have if no family member had matched in
    the first place.

    Returns the person who ultimately confirmed, together with "family" or
    "freelance". Cancels the clinic booking and raises if nobody does.
    """
    for member in (arrangement.family_member, *arrangement.remaining_family):
        if family_call.confirm_family_member(member, slot, user, clinic):
            return member, "family"

    # The same pool a freelance-first run would have searched -- the caller's
    # injected candidates in a test, or the real roster in production. Never
    # loaded eagerly: most runs never reach this fallback at all.
    freelance_pool = _resolve_freelance_pool(clinic, arrangement.freelance_pool)
    ranked = interpreter_matching.search_freelancers_in_batches(
        freelance_pool, [slot], clinic, batch_size=interpreter_matching.DEFAULT_BATCH_SIZE
    )
    for candidate in ranked:
        if interpreter_matching.confirm_interpreter(candidate, slot, clinic, user):
            return candidate, "freelance"

    _cancel_and_raise(
        user, clinic, slot, booking,
        f"Every family member and freelance interpreter who could cover "
        f"{slot.date} {slot.time} declined the final confirmation.",
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


# --- Cancellation ------------------------------------------------------------
#
# Undoes an already-SUCCEEDED booking, on the user's request -- a separate
# action from anything above, placed long after run_interpreter_mesh() has
# returned and its local ClinicCandidate/InterpreterCandidate/FamilyInterpreter
# objects are gone. `result` is the plain dict a succeeded run's
# AppointmentResult serializes to (dataclasses.asdict) -- exactly what
# api/server.py stores and what the frontend polls back -- so this works from
# a stored record alone, with no live objects from the original run required.

def cancel_appointment(result: dict, patient_name: str) -> dict:
    """Cancels the clinic appointment, then releases whoever was lined up to
    interpret it -- family or freelance, whichever `result["interpreter"]`
    says was actually confirmed. Nobody is called for a user-arranged
    interpreter: no contact details were ever collected for them, same as
    everywhere else in this design.

    Both the clinic cancel and the interpreter release are attempted
    regardless of each other's outcome: an interpreter who no-shows because
    the clinic's own system wasn't actually updated is a worse outcome than
    a clinic that still thinks the slot is taken, so releasing them doesn't
    wait on the clinic confirming first. A clinic that refuses to confirm
    the cancellation is reported, not raised -- same as everywhere else, the
    caller (and ultimately the user) needs to know to follow up directly,
    not have the whole action fail over it."""
    contact = result.get("clinic_contact") or {}
    if not contact.get("phone"):
        raise RuntimeError(
            "This booking has no clinic contact info on file -- it predates "
            "the cancellation feature, or the record is incomplete. Call the "
            "clinic directly to cancel."
        )
    clinic = ClinicCandidate(
        name=contact.get("name") or "the clinic",
        phone=contact["phone"],
        zipcode=contact.get("zipcode", ""),
        clinic_type="",
        address=contact.get("address"),
    )
    slot_dict = result.get("appointment_slot") or {}
    slot = ClinicSlot(date=slot_dict.get("date", ""), time=slot_dict.get("time", ""))
    booking_reference = (result.get("appointment") or {}).get("booking_reference")

    clinic_cancelled = clinic_call.cancel_booking(clinic, slot, patient_name, booking_reference)
    evidence = [
        f"Clinic {clinic.name} "
        + (
            "confirmed the cancellation."
            if clinic_cancelled
            else "did NOT confirm the cancellation -- call them directly to make sure."
        )
    ]

    interpreter = result.get("interpreter") or {}
    tier = interpreter.get("tier")
    interpreter_released = None
    release_reason = f"the {slot.date} {slot.time} appointment at {clinic.name} was cancelled"
    if tier == "freelance" and interpreter.get("phone"):
        candidate = InterpreterCandidate(name=interpreter.get("name", ""), phone=interpreter["phone"])
        interpreter_matching.send_release(candidate, release_reason)
        interpreter_released = True
        evidence.append(f"{candidate.name} was called and told the engagement is cancelled.")
    elif tier == "family" and interpreter.get("phone"):
        member = FamilyInterpreter(
            name=interpreter.get("name", ""), relation=interpreter.get("relation", ""),
            phone=interpreter["phone"],
        )
        family_call.release_family_member(member, patient_name, clinic, slot, release_reason)
        interpreter_released = True
        evidence.append(f"{member.name} was called and told the appointment is cancelled.")
    else:
        interpreter_released = False
        evidence.append(
            "No interpreter to release -- the user arranged their own, with "
            "no contact details on file."
            if tier == "user_arranged"
            else "No interpreter contact on file to release."
        )

    return {
        "clinic_cancelled": clinic_cancelled,
        "interpreter_released": interpreter_released,
        "evidence": evidence,
    }
