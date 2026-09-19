"""
Everything that talks to a clinic. Two distinct moments:

  search_clinics()  -- Step 2, "which nearby clinic accepts insurance at all
                       AND has a slot the user can actually attend?" Up to 10
                       clinics (found by clinic_lookup.py, not supplied by the
                       user), called one at a time nearest-first, stopping at
                       the first match.
  book_slot()       -- Step 5, the real booking, placed only after the user
                       has approved the proposed appointment.
  cancel_booking()  -- undoes that booking if every freelance interpreter
                       then declines the final confirmation.

What each leg may say about the patient differs, deliberately:

  - The SEARCH leg asks only whether the clinic accepts insurance as a matter
    of policy, and volunteers nothing about the patient. Ten clinics get
    called and nine of them are never used; none of them needs a name.
  - The BOOKING leg gives the clinic the patient's name, date of birth, age,
    phone and insurance provider + policy number. A real clinic cannot book an
    anonymous appointment, and this is the one clinic that ends up holding the
    appointment.

Ordering: the clinic is booked BEFORE any freelance interpreter is asked to
commit, because a patient booking is normally free to cancel and an
interpreter engagement isn't. Neither happens until the user approves (see
workflow/confirmation.py).
"""

from ..calle.run import CallPurpose, call_and_wait

from . import calendar
from .types import (
    ClinicCandidate,
    ClinicSlot,
    UserInput,
    append_note,
    clean_strings,
    distance_sort_key,
)

_REQUIREMENTS_FIELD = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Anything the clinic says is required to book or attend, for example "
        "a referral, photo ID, forms or a deposit. Empty if none."
    ),
}

_NOTES_FIELD = {
    "type": "string",
    "description": "Any other concern or condition the clinic raised. Empty if none.",
}

CLINIC_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "accepts_insurance": {"type": "boolean"},
        "available_slots": {
            "type": "array",
            "description": "Open appointment times inside the patient's free windows.",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "24-hour HH:MM, e.g. 14:30"},
                },
            },
        },
        "requirements": _REQUIREMENTS_FIELD,
        "additional_notes": _NOTES_FIELD,
    },
}

CLINIC_BOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "booked": {"type": "boolean"},
        "confirmed_by": {"type": "string"},
        "booking_reference": {"type": "string"},
        "blocked_reason": {
            "type": "string",
            "description": (
                "If the clinic would not book, why (for example it needs a "
                "referral first). Empty if it booked."
            ),
        },
        "requirements": _REQUIREMENTS_FIELD,
        "additional_notes": _NOTES_FIELD,
    },
}

CLINIC_CANCEL_SCHEMA = {
    "type": "object",
    "properties": {
        "cancelled": {"type": "boolean"},
        "additional_notes": _NOTES_FIELD,
    },
}


def ask_insurance_then_slots(
    clinic: ClinicCandidate, user: UserInput, batch_position: int | None = None
) -> ClinicCandidate:
    """
    ONE call per clinic. The task asks the insurance-acceptance question
    first and only asks about open slots if the answer is yes, so a clinic
    that doesn't take insurance is never asked about appointments -- the
    branch happens inside the conversation, which is why this is one call
    and not two (and why the spec's 10-clinic ceiling costs ~10 calls).

    This call shares the insurance PROVIDER (if asked) and the user's free
    times. Everything else about the patient is held back for the booking
    call, once a clinic is actually chosen.

    Mutates and returns `clinic`, mirroring how the freelance search fills in
    rate/minimum-hours.
    """
    task = (
        "Start by saying: 'I am calling on behalf of a deaf or hard of hearing "
        "person. Please help accommodate their needs as best you can.' "
        "Ask whether the clinic accepts the patient's insurance. If they ask "
        f"which insurance it is, say {user.insurance.provider_name}. If they ask "
        "for any other patient details, say those will be provided when the "
        "appointment is booked. "
        "If they do NOT accept it, thank them and end the call without asking "
        "anything else. "
        "Only if they DO accept it, say the patient is free at these times: "
        f"{calendar.describe_windows(user.free_windows)}. Ask whether the clinic "
        "can see them at any of those times. If it can, get the specific open "
        "appointment times inside those windows and return each one as a date "
        "(YYYY-MM-DD) and a time (24-hour HH:MM). If it cannot, thank them and "
        "end the call. "
        "This call is an availability check ONLY: do not book, reserve or hold "
        "any appointment, even if the clinic offers to. If they offer, say "
        "someone will call back to book once the patient has confirmed. "
        "Also record anything the clinic says is required to book or attend "
        "(for example a referral, photo ID, forms or a deposit) and any other "
        "concern they raise. Do not treat these as a reason to end the call."
    )

    result = call_and_wait(
        task, clinic.phone, CLINIC_SEARCH_SCHEMA,
        batch_position=batch_position, purpose=CallPurpose.CLINIC_SEARCH,
    )
    if not result.task_completed:
        # No answer / voicemail / the call never got an answer out of them.
        # Explicitly NOT the same as "declines insurance": leaving
        # accepts_insurance as None keeps those two distinguishable in the
        # evidence trail, where a bare .get() would silently collapse a
        # voicemail into a policy decline.
        return clinic
    clinic.accepts_insurance = bool(result.structured_result.get("accepts_insurance", False))
    clinic.requirements = clean_strings(result.structured_result.get("requirements"))
    clinic.notes = append_note(clinic.notes, result.structured_result.get("additional_notes"))
    if not clinic.accepts_insurance:
        return clinic  # skipped without ever being asked about slots
    raw_slots = result.structured_result.get("available_slots") or []
    clinic.offered_slots = [
        ClinicSlot(date=s["date"], time=s["time"])
        for s in raw_slots
        if isinstance(s, dict) and "date" in s and "time" in s
    ]
    return clinic


def search_clinics(
    clinics: list[ClinicCandidate],
    user: UserInput,
    max_clinics: int = 10,
) -> "tuple[ClinicCandidate | None, list[ClinicSlot]]":
    """
    Step 2. Calls clinics one at a time, nearest first, capped at
    `max_clinics`, and stops at the first that accepts insurance and offers a
    slot the user can attend. The calls are sequential, so the nearest match
    is by construction the first one -- nothing is gained by calling further
    down the list once a clinic has matched.

    Satisficing, not optimizing: the nearest clinic that works wins, even if a
    farther one would have offered a better time.

    Distances can be None (a ZIP outside the Nevada centroid table), so the
    ordering goes through types.distance_sort_key -- a bare
    `key=lambda c: c.distance_miles` raises TypeError the moment one is.
    """
    shortlist = sorted(clinics, key=lambda c: distance_sort_key(c.distance_miles))[:max_clinics]
    # `position` is the clinic's index in the list; in real mode it picks the
    # demo test line, cycling through the three.
    for position, clinic in enumerate(shortlist):
        ask_insurance_then_slots(clinic, user, batch_position=position)
        if not clinic.accepts_insurance:
            continue
        slots = calendar.matched_slots(clinic.offered_slots, user.free_windows)
        if slots:
            return clinic, slots
    return None, []


def book_slot(clinic: ClinicCandidate, slot: ClinicSlot, user: UserInput) -> dict:
    """Step 5. The real booking, placed only after the user has approved the
    proposal -- which is why the prompt can say the patient has been told what
    the clinic said is required.

    This is the one clinic call that identifies the patient: the clinic is
    given the name, date of birth, age, phone and insurance details it needs
    to put an appointment in its book."""
    informed = (
        f"When we checked availability, the clinic said these are required for "
        f"the appointment: {'; '.join(clinic.requirements)}. The patient has "
        f"been told about them and wants to proceed, so book the appointment "
        f"even though they are still outstanding, if the clinic allows it. "
        if clinic.requirements
        else ""
    )
    task = (
        "Start by saying: 'I am calling on behalf of a deaf or hard of hearing "
        "person. We called earlier to check availability, and we would like to "
        "book the appointment.' "
        f"Book the {slot.date} {slot.time} appointment. "
        "Give them the patient's details when they ask for them: "
        f"{user.patient_summary()}. "
        f"{informed}"
        "If the clinic will not book until something is done first, do not "
        "book: thank them, end the call, and give the reason. Otherwise book "
        "it, and list anything the clinic says is still outstanding. "
        "Confirm the booking is actually made and get the name of whoever "
        "confirmed it, plus a booking reference if they have one."
    )
    result = call_and_wait(
        task, clinic.phone, CLINIC_BOOK_SCHEMA, purpose=CallPurpose.CLINIC_BOOK
    )
    _require_booked(result, f"{slot.date} {slot.time}")
    return result.structured_result


def _require_booked(result, slot_description: str) -> None:
    """A failed, voicemail'd, or declined clinic call must never be treated
    as a successful booking. The clinic is booked before any interpreter is
    asked to commit, so a failure here leaves nothing to undo."""
    if result.task_completed and result.structured_result.get("booked"):
        return
    detail = "Nothing was booked and no interpreter was confirmed, so nothing needs cancelling."
    reason = result.structured_result.get("blocked_reason")
    clinic_said = f" The clinic said: {reason.strip()}." if isinstance(reason, str) and reason.strip() else ""
    still_needed = clean_strings(result.structured_result.get("requirements"))
    needed = f" Still required before they will book: {'; '.join(still_needed)}." if still_needed else ""
    raise RuntimeError(
        f"Clinic booking for {slot_description} did not succeed "
        f"(task_completed={result.task_completed}, "
        f"booked={result.structured_result.get('booked')!r}).{clinic_said}{needed} {detail}"
    )


def cancel_booking(
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    user: UserInput,
    booking_reference: str | None = None,
) -> bool:
    """Undoes a booking made by book_slot(), for when every freelance
    interpreter declines the final confirmation and the appointment they were
    meant to cover shouldn't stand. Returns whether the clinic confirmed the
    cancellation, so the caller can tell the user if it didn't."""
    reference = f", booking reference {booking_reference}" if booking_reference else ""
    task = (
        "Start by saying: 'I am calling on behalf of a deaf or hard of hearing "
        "person. We booked an appointment with you earlier and need to cancel "
        "it.' "
        f"Cancel the {slot.date} {slot.time} appointment for {user.name}{reference}. "
        "Confirm it is actually cancelled."
    )
    result = call_and_wait(
        task, clinic.phone, CLINIC_CANCEL_SCHEMA, purpose=CallPurpose.CLINIC_CANCEL
    )
    return bool(result.task_completed and result.structured_result.get("cancelled"))
