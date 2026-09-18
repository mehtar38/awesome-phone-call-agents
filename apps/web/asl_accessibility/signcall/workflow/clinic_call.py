"""
Everything that talks to a clinic. Two distinct moments:

  search_clinics()  -- Step 2, "which nearby clinic accepts insurance at all
                       AND has a slot the user can actually attend?" Up to 10
                       clinics (found by clinic_lookup.py, not supplied by the
                       user), nearest-first, called in batches of 3, stopping
                       at the first batch with a match.
  book_slot()       -- Step 4, the real booking, placed only once an
                       interpreter has been secured.

What each leg may say about the patient differs, deliberately:

  - The SEARCH leg asks only whether the clinic accepts insurance as a matter
    of policy, and volunteers nothing about the patient. Ten clinics get
    called and nine of them are never used; none of them needs a name.
  - The BOOKING leg gives the clinic the patient's name, date of birth, age,
    phone and insurance provider + policy number. A real clinic cannot book an
    anonymous appointment, and this is the one clinic that ends up holding the
    appointment.

Note on ordering (changed, deliberately): this module used to book the clinic
BEFORE any interpreter was asked to commit, because a patient booking is
normally free to cancel and an interpreter engagement isn't. The finalized
sequence inverts that -- the interpreter is confirmed first and the clinic is
called back afterwards -- as an accepted, explicit trade (see the "Flagged
tension" callout in CALL-E Hackathon Ideas.md and README's known
limitations). The consequence lives in _require_booked() below.
"""

from ..calle.run import call_and_wait

from . import calendar
from .types import ClinicCandidate, ClinicSlot, TimeWindow, UserInput, distance_sort_key

CLINIC_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "accepts_insurance": {"type": "boolean"},
        "available_slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"date": {"type": "string"}, "time": {"type": "string"}},
            },
        },
    },
}

CLINIC_BOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "booked": {"type": "boolean"},
        "confirmed_by": {"type": "string"},
        "booking_reference": {"type": "string"},
    },
}


def ask_insurance_then_slots(
    clinic: ClinicCandidate, window_days: int, batch_position: int | None = None
) -> ClinicCandidate:
    """
    ONE call per clinic. The task asks the insurance-acceptance question
    first and only asks about open slots if the answer is yes, so a clinic
    that doesn't take insurance is never asked about appointments -- the
    branch happens inside the conversation, which is why this is one call
    and not two (and why the spec's 10-clinic ceiling costs ~10 calls).

    The insurance question is about the CLINIC'S OWN acceptance policy. This
    leg volunteers nothing about the patient -- their name, date of birth and
    insurance details are for the booking call, once a clinic is actually
    chosen.

    Mutates and returns `clinic`, mirroring how the freelance search fills in
    rate/minimum-hours.
    """
    task = (
        f"Call this clinic on behalf of a deaf/Hard of hearing person. First ask whether they accept insurance at all "
        f"-- this is about the clinic's own policy; do not give or discuss "
        f"any specific patient's details on this call. If they do NOT accept "
        f"insurance, thank them and end the call without asking anything "
        f"else. Only if they DO accept insurance, ask what appointment slots "
        f"are open in the next {window_days} days and return each option as "
        f"a date and a time."
    )
    
    result = call_and_wait(task, clinic.phone, CLINIC_SEARCH_SCHEMA, batch_position=batch_position)
    if not result.task_completed:
        # No answer / voicemail / the call never got an answer out of them.
        # Explicitly NOT the same as "declines insurance": leaving
        # accepts_insurance as None keeps those two distinguishable in the
        # evidence trail, where a bare .get() would silently collapse a
        # voicemail into a policy decline.
        return clinic
    clinic.accepts_insurance = bool(result.structured_result.get("accepts_insurance", False))
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
    user_free_windows: list[TimeWindow],
    window_days: int,
    max_clinics: int = 10,
    batch_size: int = 3,
) -> "tuple[ClinicCandidate | None, list[ClinicSlot]]":
    """
    Step 2. Nearest-first, capped at `max_clinics`, called `batch_size` at a
    time. A batch is worked through in full (every clinic in it is called)
    because the tie-break is a comparison WITHIN the batch; as soon as a
    batch yields at least one clinic that accepts insurance and has a slot
    the user can attend, the search stops -- no further clinics are called --
    and the nearest of that batch's matches wins.

    Satisficing, not optimizing: this is an intentional trade of
    thoroughness for call-budget control (10 clinics = 4 batches = ~10 real
    calls worst case). Two runs against the same clinic pool can pick
    different clinics depending on batch order.

    Distances can be None (a ZIP outside the Nevada centroid table), so every
    comparison here goes through types.distance_sort_key -- a bare
    `key=lambda c: c.distance_miles` raises TypeError the moment one is.
    """
    shortlist = sorted(clinics, key=lambda c: distance_sort_key(c.distance_miles))[:max_clinics]
    for start in range(0, len(shortlist), batch_size):
        batch = shortlist[start : start + batch_size]
        matches: list[tuple[ClinicCandidate, list[ClinicSlot]]] = []
        for position, clinic in enumerate(batch):
            ask_insurance_then_slots(clinic, window_days, batch_position=position)
            if not clinic.accepts_insurance:
                continue
            slots = calendar.matched_slots(clinic.offered_slots, user_free_windows)
            if slots:
                matches.append((clinic, slots))
        if matches:
            # sorted() is stable and the shortlist is already nearest-first,
            # so an exact distance tie falls back to that original order.
            return min(matches, key=lambda m: distance_sort_key(m[0].distance_miles))
    return None, []


def book_slot(
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    user: UserInput,
    interpreter_name: str | None = None,
) -> dict:
    """Step 4. The real booking, placed AFTER an interpreter has been secured
    (see the module docstring on why that ordering changed).

    This is the one call that identifies the patient: the clinic is given the
    name, date of birth, age, phone and insurance details it needs to put an
    appointment in its book."""
    task = (
        f"Call this clinic and book the {slot.date} {slot.time} appointment. "
        f"Give them the patient's details when they ask for them: "
        f"{user.patient_summary()}. Confirm the booking is actually made and "
        f"get the name of whoever confirmed it, plus a booking reference if "
        f"they have one."
    )
    result = call_and_wait(task, clinic.phone, CLINIC_BOOK_SCHEMA)
    _require_booked(result, f"{slot.date} {slot.time}", interpreter_name)
    return result.structured_result


def _require_booked(result, slot_description: str, interpreter_name: str | None = None) -> None:
    """A failed, voicemail'd, or declined clinic call must never be treated
    as a successful booking.

    Under the finalized sequence this failure is the flagged, knowingly
    accepted risk rather than a near-miss: when an interpreter was secured by
    the agent, they have ALREADY committed for real by the time this runs, so
    a failure here means a fee-bearing engagement exists with no appointment
    behind it. Nothing releases them automatically (send_release() is
    deliberately unwired), so the message says so outright.

    When the user brought their own interpreter, the agent confirmed nobody --
    and the message must not claim otherwise."""
    if result.task_completed and result.structured_result.get("booked"):
        return
    detail = (
        f"{interpreter_name} has ALREADY confirmed for this slot and is not "
        f"released automatically -- contact them directly to cancel before any "
        f"cancellation window closes."
        if interpreter_name
        else "No interpreter was engaged by the agent on this run, so nothing "
             "fee-bearing is outstanding -- but nothing was booked either."
    )
    raise RuntimeError(
        f"Clinic booking for {slot_description} did not succeed "
        f"(task_completed={result.task_completed}, "
        f"booked={result.structured_result.get('booked')!r}). {detail}"
    )


def cancel_booking(clinic_phone: str, booking_reference: str, batch_position: int | None = None) -> None:
    """Currently unreferenced. Its caller was the old ordering's
    all-decline path (cancel the clinic booking when every interpreter said
    no); under the finalized sequence no booking exists yet at that point.
    Kept for the rare-reversal path, which is still unwired."""
    task = f"Call this clinic and cancel booking reference {booking_reference}."
    call_and_wait(task, clinic_phone, {"type": "object"}, batch_position=batch_position)
