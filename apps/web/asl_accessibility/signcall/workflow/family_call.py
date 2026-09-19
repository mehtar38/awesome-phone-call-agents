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
"""

from ..calle.run import CallPurpose, call_and_wait

from .types import ClinicSlot, FamilyInterpreter, UserInput, append_note

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


def call_family_in_order(
    family: list[FamilyInterpreter], matched_slots: list[ClinicSlot], user: UserInput
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
    """
    if not matched_slots:
        return None, None
    slot_keys = [s.key() for s in matched_slots]
    for position, member in enumerate(family):
        task = (
            f"Ask if you are speaking with {member.name}. Say that you are calling on "
            f"behalf of {user.name}, who is deaf or hard of hearing. Ask whether they "
            f"are free to interpret at any of these specific appointment "
            f"times: {', '.join(slot_keys)}. Return every one of those times "
            f"they can cover, each written back exactly as given "
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
