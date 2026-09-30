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
        "confirmed_date": {
            "type": "string",
            "description": (
                "YYYY-MM-DD. Only set when booked=true, and must be the exact "
                "date that was asked for -- never a different date the clinic "
                "offered instead."
            ),
        },
        "confirmed_time": {
            "type": "string",
            "description": (
                "24-hour HH:MM. Only set when booked=true, and must be the "
                "exact time that was asked for -- never a different time the "
                "clinic offered instead."
            ),
        },
        "confirmed_by": {"type": "string"},
        "booking_reference": {"type": "string"},
        "blocked_reason": {
            "type": "string",
            "description": (
                "If the clinic would not book the exact requested date and "
                "time -- including because it offered a DIFFERENT date or "
                "time instead -- why. Empty if it booked the exact slot asked for."
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
    to put an appointment in its book.

    The user approved a SPECIFIC date and time -- everyone downstream
    (family, interpreter, the confirmation texts) is told exactly that slot,
    not whatever the clinic ends up actually booking. So a different date or
    time offered on this call is refused outright, not accepted as a
    substitute: see the task text and _require_booked() below.
    """
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
        f"Book the {slot.date} {slot.time} appointment -- and ONLY that "
        f"exact date and time, no other. "
        "Give them the patient's details when they ask for them: "
        f"{user.patient_summary()}. "
        f"{informed}"
        f"If {slot.date} {slot.time} is not available, or the clinic offers "
        f"any OTHER date or time instead, do NOT accept, hold, or book that "
        f"alternative under any circumstances. Treat this exactly like a "
        f"refusal to book at all: thank them, end the call, and put the "
        f"reason in blocked_reason -- if they offered a different date or "
        f"time, say so there and name it, so the patient can decide for "
        f"themselves whether to take it. "
        "Only if they can book that exact date and time: confirm the "
        "booking is actually made, return the date and time they confirmed "
        "as confirmed_date (YYYY-MM-DD) and confirmed_time (24-hour HH:MM) "
        "-- these must match what was asked for -- get the name of whoever "
        "confirmed it and a booking reference if they have one, and list "
        "anything the clinic says is still outstanding."
    )
    result = call_and_wait(
        task, clinic.phone, CLINIC_BOOK_SCHEMA, purpose=CallPurpose.CLINIC_BOOK
    )
    _require_booked(result, slot)
    return result.structured_result


def _require_booked(result, slot: ClinicSlot) -> None:
    """A failed, voicemail'd, or declined clinic call must never be treated
    as a successful booking -- and neither may a booking for a DIFFERENT date
    or time than the one the user actually approved, even if the clinic
    agent reports booked=true. A real, confirmed bug: the calling agent once
    accepted a clinic's alternate-day offer and reported success, and nothing
    here checked WHICH day was actually confirmed -- the workflow went on to
    tell the interpreter and the user about the original, never-booked slot.

    The clinic is booked before any interpreter is asked to commit, so a
    failure here leaves nothing of OURS to undo -- but the clinic's own
    system may already reflect the alternate slot from earlier in the same
    call, which is called out explicitly below."""
    structured = result.structured_result
    booked = bool(result.task_completed and structured.get("booked"))
    confirmed_date = structured.get("confirmed_date")
    confirmed_time = structured.get("confirmed_time")
    mismatched = booked and (
        (isinstance(confirmed_date, str) and confirmed_date != slot.date)
        or (isinstance(confirmed_time, str) and confirmed_time != slot.time)
    )
    if booked and not mismatched:
        return

    slot_description = f"{slot.date} {slot.time}"
    if mismatched:
        detail = (
            f"REJECTED: the clinic reported booking "
            f"{confirmed_date or slot.date} {confirmed_time or slot.time} "
            f"instead -- only the exact requested time may be accepted, so "
            f"this is treated as not booked. No interpreter was confirmed. "
            f"If the clinic's own system was already updated during the "
            f"call, call them directly to make sure nothing is on their "
            f"books for the wrong day."
        )
    else:
        detail = "Nothing was booked and no interpreter was confirmed, so nothing needs cancelling."
    reason = structured.get("blocked_reason")
    clinic_said = f" The clinic said: {reason.strip()}." if isinstance(reason, str) and reason.strip() else ""
    still_needed = clean_strings(structured.get("requirements"))
    needed = f" Still required before they will book: {'; '.join(still_needed)}." if still_needed else ""
    raise RuntimeError(
        f"Clinic booking for {slot_description} did not succeed "
        f"(task_completed={result.task_completed}, "
        f"booked={structured.get('booked')!r}).{clinic_said}{needed} {detail}"
    )


def cancel_booking(
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    patient_name: str,
    booking_reference: str | None = None,
) -> bool:
    """Undoes a booking made by book_slot() -- either because every
    interpreter who might have covered it (family, then freelance) declined
    the final confirmation, or because the user cancelled an already-booked
    appointment outright (see appointment.cancel_appointment()). Returns
    whether the clinic confirmed the cancellation, so the caller can tell
    the user if it didn't.

    Takes `patient_name` rather than a full UserInput: this is the one piece
    of the patient's identity a cancel call actually needs, and a
    user-initiated cancellation may run long after the original UserInput
    object existed -- only the succeeded run's own stored result survives
    that long (see cancel_appointment()'s docstring)."""
    reference = f", booking reference {booking_reference}" if booking_reference else ""
    task = (
        "Start by saying: 'I am calling on behalf of a deaf or hard of hearing "
        "person. We booked an appointment with you earlier and need to cancel "
        "it.' "
        f"Cancel the {slot.date} {slot.time} appointment for {patient_name}{reference}. "
        "Confirm it is actually cancelled."
    )
    result = call_and_wait(
        task, clinic.phone, CLINIC_CANCEL_SCHEMA, purpose=CallPurpose.CLINIC_CANCEL
    )
    return bool(result.task_completed and result.structured_result.get("cancelled"))
