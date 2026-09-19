"""
Step 3B: freelance interpreter sourcing, and the binding confirm.

This is a SATISFICING batch search,
not a global optimization: candidates are called in batches of 3 -- all three
at the same time -- against the already-matched clinic slots, each also asked
their hourly rate, and the
search stops at the first batch with at least one match. "Cheapest" is only
ever a comparison within that batch -- two runs against the same pool can
book different people depending on batch order. That's the documented trade
for call-budget control.

Ordering: confirm_interpreter() is the single binding ask, and it happens last
-- after the user has approved the appointment and the clinic slot is booked.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..calle.run import CallPurpose, call_and_wait

from .clinic_lookup import zip_distance_miles
from .types import ClinicSlot, InterpreterCandidate, append_note, distance_sort_key

_NOTES_FIELD = {
    "type": "string",
    "description": "Any concern or condition they raised. Empty if none.",
}

ROSTER_PATH = Path(__file__).resolve().parent.parent / "data" / "interpreters_nv.json"

INTERPRETER_AVAILABILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "coverable_slots": {"type": "array", "items": {"type": "string"}},
        "rate_per_hour": {"type": "number"},
        "minimum_hours": {"type": "number"},
        "additional_notes": _NOTES_FIELD,
    },
}

INTERPRETER_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {"confirmed": {"type": "boolean"}, "additional_notes": _NOTES_FIELD},
}


_roster_cache: list[dict] | None = None


def _roster() -> list[dict]:
    """Parsed ONCE and cached as raw dicts, never as InterpreterCandidate
    objects: the batch search writes each candidate's rate, slots and
    availability back into the object in place, so a cached object graph
    would hand the next run a roster dirtied by the previous one."""
    global _roster_cache
    if _roster_cache is None:
        _roster_cache = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))["interpreters"]
    return _roster_cache


def load_candidates_within_radius(
    clinic_zip: str, radius_miles: float = 15.0
) -> list[InterpreterCandidate]:
    """
    Bounds *who gets called* -- not a rank. Proximity's job is to keep the
    search list practical; rate, not distance, decides who gets booked
    (see rank_by_rate below).

    Backed by data/interpreters_nv.json: 30 SYNTHETIC interpreters modelled on
    the structure of public ASL interpreter directories, half of them in Las
    Vegas and half spread across the rest of Nevada. Real directory data
    (RID's registry, state referral lists) can't be used to place live demo
    calls, so the data is invented and every number sits in the reserved
    555-01xx fictional block -- the shape and distribution are real, the
    people are not. Rates and minimum hours are deliberately absent from the
    roster: they're asked on the call, never assumed.

    `clinic_zip` is the MATCHED clinic's ZIP, which only exists because the
    clinic is now discovered (Step 2) rather than supplied -- this function
    was un-wireable before that.
    """
    candidates = []
    for record in _roster():
        distance = zip_distance_miles(clinic_zip, record["zipcode"])
        if distance is None or distance > radius_miles:
            continue
        candidates.append(
            InterpreterCandidate(
                name=record["name"],
                phone=record["phone_number"],
                zipcode=record["zipcode"],
                city=record.get("city", ""),
                age=record.get("age"),
                expertise=list(record.get("expertise", [])),
                distance_miles=distance,
            )
        )
    candidates.sort(key=lambda c: distance_sort_key(c.distance_miles))
    return candidates


def _ask_availability_and_rate(
    candidate: InterpreterCandidate, slot_keys: list[str], batch_position: int | None = None
) -> bool:
    """One non-binding availability check -- no hold requested. Mutates the
    candidate with whatever they actually said. Returns whether they count
    as a match for this batch."""
    task = (
        f"Tell that you are calling on behalf of a hard of hearing/deaf person. Ask this ASL interpreter whether they're available for any of "
        f"these appointment times: {', '.join(slot_keys)}. Return every one "
        f"of those times they can cover, each written back exactly as given "
        f"(YYYY-MM-DD HH:MM). If they can cover any of them, also get their "
        f"hourly rate and their minimum billable hours for the visit. This "
        f"is an availability check only -- do not book or place a hold."
    )

    result = call_and_wait(
        task, candidate.phone, INTERPRETER_AVAILABILITY_SCHEMA,
        batch_position=batch_position, purpose=CallPurpose.INTERPRETER_AVAILABILITY,
    )
    if not result.task_completed:
        return False  # no answer / declined to engage at all
    candidate.notes = append_note(candidate.notes, result.structured_result.get("additional_notes"))
    raw = result.structured_result.get("coverable_slots") or []
    coverable = [s for s in raw if isinstance(s, str) and s in slot_keys]
    if not coverable:
        return False
    candidate.rate_per_hour = result.structured_result.get("rate_per_hour")
    candidate.minimum_hours = result.structured_result.get("minimum_hours")
    candidate.coverable_slots = coverable  # store the actual answer, not just
                                            # a truthiness check -- see the
                                            # field's docstring in types.py
    if candidate.rate_per_hour is None:
        # Available but gave no rate. Deliberately NOT counted as a match:
        # the whole point of asking is to compare rates within the batch, and
        # letting this through would mean confirm_interpreter() reading
        # "$None/hr, Nonehr minimum" out loud to a real person. The search
        # continues to the next batch instead.
        candidate.can_make_target_slot = None
        return False
    candidate.can_make_target_slot = True
    return True


def rank_by_rate(matches: list[InterpreterCandidate]) -> list[InterpreterCandidate]:
    """The finalized workflow's tie-break: "the cheapest by rate among that
    batch's matches is confirmed." Every candidate here has already been
    filtered to a non-None rate by _ask_availability_and_rate. sorted() is
    stable, so an exact rate tie falls back to the caller's candidate
    order."""
    return sorted(matches, key=lambda c: c.rate_per_hour)


def search_freelancers_in_batches(
    candidates: list[InterpreterCandidate],
    matched_slots: list[ClinicSlot],
    batch_size: int = 3,
) -> list[InterpreterCandidate]:
    """
    Step 3B. Calls candidates `batch_size` at a time against the matched
    slots, the whole batch in parallel: the cheapest-by-rate comparison is
    within the batch, so every answer is needed, and nothing is gained by
    waiting on one before dialling the next. The first batch with at least
    one match ends the search -- no further interpreters are called -- and its
    matches come back sorted cheapest-first, however the answers arrived.

    An exception on any call ends the search with that exception, as it would
    if the calls ran one after another.
    """
    if not matched_slots:
        return []
    slot_keys = [s.key() for s in matched_slots]
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            answers = list(
                pool.map(
                    lambda item: _ask_availability_and_rate(
                        item[1], slot_keys, batch_position=item[0]
                    ),
                    enumerate(batch),
                )
            )
        matches = [candidate for candidate, matched in zip(batch, answers) if matched]
        if matches:
            return rank_by_rate(matches)
    return []


def slot_for_candidate(
    candidate: InterpreterCandidate, matched_slots: list[ClinicSlot]
) -> "ClinicSlot | None":
    """The slot this specific candidate gets confirmed for: the first
    matched slot THEY named. This is what keeps a cheaper candidate who can
    only do Monday from being booked for Thursday -- a real, confirmed bug
    in an earlier design, now structurally impossible because the slot is
    derived from the candidate rather than chosen alongside them."""
    for slot in matched_slots:
        if slot.key() in candidate.coverable_slots:
            return slot
    return None


def rank_by_cost(responded: list[InterpreterCandidate]) -> list[InterpreterCandidate]:
    """
    Ranks by TOTAL cost (rate x minimum hours) rather than hourly rate.
    Currently unreferenced: the finalized workflow's tie-break is explicitly
    "cheapest by rate", which rank_by_rate above implements. Kept as-is,
    including its known behaviour of dropping any candidate whose
    minimum_hours is null (that reads as "can't compute cost" rather than
    "no minimum applies") -- see README's known limitations.
    """
    ranked = [c for c in responded if c.total_cost() is not None]
    ranked.sort(key=lambda c: c.total_cost())
    return ranked


def confirm_interpreter(candidate: InterpreterCandidate, slot: ClinicSlot) -> bool:
    """
    The one moment an interpreter is asked to commit for real. They were
    asked non-bindingly during the batch search; this is a live yes/no on
    the specific slot and price.

    This happens after the user has approved and the clinic slot is booked --
    see the module docstring.
    """
    task = (
        f"Call this interpreter back and confirm: can they do "
        f"{slot.date} {slot.time} at ${candidate.rate_per_hour}/hr, "
        f"{candidate.minimum_hours}hr minimum? Get a clear yes or no."
    )
    result = call_and_wait(
        task, candidate.phone, INTERPRETER_CONFIRM_SCHEMA,
        purpose=CallPurpose.INTERPRETER_CONFIRM,  # sticky line
    )
    candidate.notes = append_note(candidate.notes, result.structured_result.get("additional_notes"))
    return bool(result.structured_result.get("confirmed", False))


def send_release(candidate: InterpreterCandidate, reason: str) -> None:
    """Rare reversal only: the clinic booking fell through after this
    interpreter already said yes. A genuine engagement existed, so this is a
    real release, not a no-op. Currently unreferenced: the clinic is booked
    before any interpreter commits, so nothing reaches this state today."""
    task = f"Call this interpreter and let them know the appointment was cancelled: {reason}."
    call_and_wait(
        task, candidate.phone, {"type": "object"},
        batch_position=0, purpose=CallPurpose.INTERPRETER_RELEASE,
    )
