"""
Step 3A: the family tier.

Family is a definite, pre-registered, ORDERED list, reached by phone ONLY (no
share link, no channel preference, no urgency branching), and checked AFTER a
clinic and its matched slots already exist -- not before. Each member is called
one at a time, in list order, and asked which of those already-matched slots
they can cover; the first one who can cover any of them is locked in and nobody
further is called. Once the appointment is actually booked, whoever was
secured gets a confirmation text (Step 4) -- the call-only rule governs how
they are SECURED, not whether they're told the outcome.

This lives in its own module rather than in reminders.py because it IS a
CALL-E call. reminders.py is for the non-CALL-E side channels.

Step 5 adds a second family call: `confirm_family_member()`, the binding ask
placed after the clinic is actually booked. A "yes" from call_family_in_order
above was given to a slot that wasn't booked yet; this call is what makes it
real -- it mirrors interpreter_matching.confirm_interpreter() and is asked
for the same reason.

`release_family_member()` is the reverse of that binding ask: the user
cancelled an already-booked appointment, so whoever was confirmed to
interpret it needs to be told it's off -- mirrors
interpreter_matching.send_release(). See appointment.cancel_appointment().
"""

from ..calle.run import CallPurpose, call_and_wait

from .types import ClinicCandidate, ClinicSlot, FamilyInterpreter, UserInput, append_note

FAMILY_SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "coverable_slots": {
            "type": "array",
            "items": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
        },
        "additional_notes": {
            "type": "string",
            "description": "Any concern or condition they raised. Empty if none.",
        },
    },
}

_NOTES_FIELD = {
    "type": "string",
    "description": "Any concern or condition they raised. Empty if none.",
}

FAMILY_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {"confirmed": {"type": "boolean"}, "additional_notes": _NOTES_FIELD},
}


def call_family_in_order(
    family: list[FamilyInterpreter],
    matched_slots: list[ClinicSlot],
    user: UserInput,
    clinic: ClinicCandidate,
) -> "tuple[FamilyInterpreter | None, ClinicSlot | None]":
    """
    Returns the first member who can cover a matched slot, together with the
    slot they're locked in for (the first matched slot they named, in the
    clinic's own slot order). Returns (None, None) if the whole list is
    exhausted with no match -- the caller then moves on to freelance
    sourcing.

    Every registered member is eligible: the appointment-sensitivity gate
    that used to decide whether family could be considered at all is gone
    from this build. Whether family is used is the user's call, expressed by
    who they register and by has_interpreter.

    Gives the clinic's name and location -- unlike the freelance availability
    ask, there's no reason to withhold it here, since family already knows
    who the patient is, and knowing where they'd need to be helps them judge
    whether they can actually make it.
    """
    if not matched_slots:
        return None, None
    slot_keys = [s.key() for s in matched_slots]
    for position, member in enumerate(family):
        task = (
            f"Ask if you are speaking with {member.name}. Say that you are calling on "
            f"behalf of {user.name}, who is deaf or hard of hearing. Ask whether they "
            f"are free to interpret at any of these specific appointment "
            f"times: {', '.join(slot_keys)}, at {clinic.name} "
            f"({clinic.location_description()}). Return every one of those "
            f"times they can cover, each written back exactly as given "
            f"(YYYY-MM-DD HH:MM). If they can't cover any of them, return an "
            f"empty list."
        )

        result = call_and_wait(
            task, member.phone, FAMILY_SLOT_SCHEMA,
            batch_position=position, purpose=CallPurpose.FAMILY_AVAILABILITY,
        )
        if not result.task_completed:
            continue  # no answer / declined to engage -- try the next member
        member.notes = append_note(member.notes, result.structured_result.get("additional_notes"))
        raw = result.structured_result.get("coverable_slots") or []
        member.coverable_slots = [s for s in raw if isinstance(s, str)]
        for slot in matched_slots:
            if slot.key() in member.coverable_slots:
                return member, slot  # locked in; stop calling the list
    return None, None


def confirm_family_member(
    member: FamilyInterpreter, slot: ClinicSlot, user: UserInput, clinic: ClinicCandidate
) -> bool:
    """The binding ask, made after the clinic is booked -- see the module
    docstring. Works equally for the member who was locked in during the
    search (a real follow-up) and for one further down the list who was
    never reached before (a first, direct ask): either way this is the live
    yes/no for the exact slot that's now actually booked, so the wording
    doesn't presuppose they were already called.

    Gives the clinic's name and location, same as the earlier availability
    ask -- this is the call that tells them whether they actually need to
    show up, so leaving out where would defeat the point of it."""
    task = (
        f"Tell {member.name} that {user.name}'s appointment has been booked "
        f"for {slot.date} {slot.time} at {clinic.name} "
        f"({clinic.location_description()}), and ask whether they can "
        f"interpret at that time. Get a clear yes or no."
    )
    result = call_and_wait(
        task, member.phone, FAMILY_CONFIRM_SCHEMA, purpose=CallPurpose.FAMILY_CONFIRM,
    )
    member.notes = append_note(member.notes, result.structured_result.get("additional_notes"))
    return bool(result.structured_result.get("confirmed", False))


FAMILY_RELEASE_SCHEMA = {"type": "object", "properties": {"additional_notes": _NOTES_FIELD}}


def release_family_member(
    member: FamilyInterpreter,
    patient_name: str,
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    reason: str,
) -> None:
    """Tells an already-confirmed family member the appointment is off. Not
    an ask -- there's nothing to decide, only something to be told -- so
    unlike every other family call there's no yes/no to return."""
    task = (
        f"Tell {member.name} that {patient_name}'s appointment for "
        f"{slot.date} {slot.time} at {clinic.name} ({clinic.location_description()}) "
        f"has been cancelled: {reason}. They do not need to interpret for it."
    )
    call_and_wait(
        task, member.phone, FAMILY_RELEASE_SCHEMA, purpose=CallPurpose.FAMILY_RELEASE,
    )
