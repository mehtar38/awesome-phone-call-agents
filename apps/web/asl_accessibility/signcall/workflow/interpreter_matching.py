"""
Step 3B: freelance interpreter sourcing, and the binding confirm.

This is a SATISFICING batch search,
not a global optimization: candidates are considered in batches of
DEFAULT_BATCH_SIZE -- against the already-matched clinic slots, each also
asked their hourly rate -- and the search stops at the first batch with at
least one match. "Cheapest" is only ever a comparison within that batch --
two runs against the same pool can book different people depending on batch
order. That's the documented trade for call-budget control. This holds
regardless of PARALLEL_BATCH_CALLS below: batch SIZE (how many are compared
before deciding) and batch DISPATCH (whether those calls go out at once or
one after another) are two different things, and only the second one
depends on what the CALL-E account can actually support concurrently.

Ordering: confirm_interpreter() is the single binding ask, and it happens last
-- after the user has approved the appointment and the clinic slot is booked.

Disclosure differs between the two calls, same rule as the clinic-search vs.
clinic-booking split: the availability ask shares the clinic's name and
location (an interpreter needs that to judge travel before quoting a rate),
but never the patient's identity, since most of a batch never gets hired. The
confirm call is only ever made to whoever IS getting hired, so it adds the
patient's name and phone number to the same clinic details.

PARALLEL_BATCH_CALLS defaults to OFF: a shared/free CALL-E line only places
ONE call at a time on the account's behalf, so dialling a batch's members at
once just means everyone past the first waits for that line to free up
(calle/run.py retries under that cap rather than failing) -- staggered in
practice, dressed up as parallel. With it off, a batch's members are dialled
one after another instead, but EVERY member of the batch is still asked --
nobody is skipped just because an earlier member already said yes -- and the
cheapest-of-the-batch decision still waits for all of them to answer before
choosing. Set SIGNCALL_FREELANCE_PARALLEL_BATCH=1 once a dedicated number is
bought (raises the cap to 10 concurrent -- see CALL-E's own account/numbers
page); nothing else in this module needs to change.
"""

import os
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..calle.run import CallPurpose, call_and_wait

from .clinic_lookup import zip_distance_miles
from .types import ClinicCandidate, ClinicSlot, InterpreterCandidate, UserInput, append_note, distance_sort_key

_NOTES_FIELD = {
    "type": "string",
    "description": "Any concern or condition they raised. Empty if none.",
}

ROSTER_PATH = Path(__file__).resolve().parent.parent / "data" / "interpreters_nv.json"

# How many freelancers are considered together per batch. The single knob
# for the batch SIZE everywhere in this module; change it here rather than
# at each call site. Unrelated to PARALLEL_BATCH_CALLS below -- that's about
# HOW a batch of this size is dialled, not how big it is.
DEFAULT_BATCH_SIZE = 2

# Whether a batch's members are dialled at once (True) or one after another
# (False, the default) -- see the module docstring for why. Read once at
# import so a single env var controls it everywhere.
PARALLEL_BATCH_CALLS = os.environ.get("SIGNCALL_FREELANCE_PARALLEL_BATCH") == "1"

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

    NOT wired into any live run. `appointment.py::_resolve_freelance_pool()`
    only ever calls workflow/interpreter_lookup.py's live Illinois registry
    lookup for an Illinois clinic, and returns an empty pool for everywhere
    else covered -- an honest "no interpreter source for this area yet"
    rather than a fabricated match (see that function's docstring). This
    function is kept as a TEST FIXTURE ONLY, exercising the batching/
    radius/rate-ranking logic below against the seeded roster so that logic
    stays covered even though nothing live reads from it. A real deployer
    with their own consented interpreter data source for another state wires
    it in at `_resolve_freelance_pool()`, the same way Illinois's is.

    Backed by data/interpreters_nv.json: 29 SYNTHETIC interpreters modelled on
    the structure of public ASL interpreter directories, 14 of them in Las
    Vegas and 15 spread across the rest of Nevada -- invented data, kept
    demo-safe with every number in the reserved 555-01xx fictional block or
    one of the operator's own test lines (see roster_load's test for which).
    Rates and minimum hours are deliberately absent from the roster: they're
    asked on the call, never assumed.

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
                source="seeded_roster",
            )
        )
    candidates.sort(key=lambda c: distance_sort_key(c.distance_miles))
    return candidates


def _ask_availability_and_rate(
    candidate: InterpreterCandidate,
    slot_keys: list[str],
    clinic: ClinicCandidate,
    batch_position: int | None = None,
) -> bool:
    """One non-binding availability check -- no hold requested. Mutates the
    candidate with whatever they actually said. Returns whether they count
    as a match for this batch.

    Gives the clinic's name and location, so an interpreter can judge travel
    before quoting a rate -- but NOT the patient's name or number. This is a
    batch ask and only one respondent gets hired, so the patient's identity
    stays back until the binding confirm call (confirm_interpreter, below),
    same rule as the clinic-search call."""
    task = (
        f"Tell that you are calling on behalf of a hard of hearing/deaf person. Ask this ASL interpreter whether they're available for any of "
        f"these appointment times: {', '.join(slot_keys)}, to interpret at "
        f"{clinic.name} ({clinic.location_description()}). Return every one "
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


def _ask_batch(
    batch: list[InterpreterCandidate], slot_keys: list[str], clinic: ClinicCandidate
) -> list[bool]:
    """Asks every member of one batch, in the SAME order as `batch`, and
    returns each one's answer. Every member is always asked -- an earlier
    yes never skips a later member -- because the cheapest-of-the-batch
    decision in search_freelancers_in_batches() needs all of them answered
    first. The only thing PARALLEL_BATCH_CALLS changes is whether these
    calls go out at once or one after another; the set of people asked and
    the decision made from their answers are identical either way."""
    if PARALLEL_BATCH_CALLS:
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            return list(
                pool.map(
                    lambda item: _ask_availability_and_rate(
                        item[1], slot_keys, clinic, batch_position=item[0]
                    ),
                    enumerate(batch),
                )
            )
    return [
        _ask_availability_and_rate(candidate, slot_keys, clinic, batch_position=position)
        for position, candidate in enumerate(batch)
    ]


def search_freelancers_in_batches(
    candidates: list[InterpreterCandidate],
    matched_slots: list[ClinicSlot],
    clinic: ClinicCandidate,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[InterpreterCandidate]:
    """
    Step 3B. Considers candidates `batch_size` at a time against the matched
    slots -- see _ask_batch() for whether that means at once or one after
    another. Either way, the cheapest-by-rate comparison is within the whole
    batch, so every answer in it is needed before deciding. The first batch
    with at least one match ends the search -- no further interpreters are
    called -- and its matches come back sorted cheapest-first, however the
    answers arrived.

    An exception on any call ends the search with that exception.
    """
    if not matched_slots:
        return []
    slot_keys = [s.key() for s in matched_slots]
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        answers = _ask_batch(batch, slot_keys, clinic)
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


def confirm_interpreter(
    candidate: InterpreterCandidate, slot: ClinicSlot, clinic: ClinicCandidate, user: UserInput
) -> bool:
    """
    The one moment an interpreter is asked to commit for real. They were
    asked non-bindingly during the batch search; this is a live yes/no on
    the specific slot and price.

    This happens after the user has approved and the clinic slot is booked --
    see the module docstring. Unlike the earlier availability ask, this call
    is only ever made to whoever is ACTUALLY about to be hired, so it's the
    right moment to give them what they need to actually show up: where the
    appointment is and who they're interpreting for.
    """
    task = (
        f"Call this interpreter back and confirm: can they do "
        f"{slot.date} {slot.time} at ${candidate.rate_per_hour}/hr, "
        f"{candidate.minimum_hours}hr minimum, interpreting for "
        f"{user.name} (phone {user.phone_number}) at {clinic.name}, "
        f"{clinic.location_description()}? Get a clear yes or no."
    )
    result = call_and_wait(
        task, candidate.phone, INTERPRETER_CONFIRM_SCHEMA,
        purpose=CallPurpose.INTERPRETER_CONFIRM,  # sticky line
    )
    candidate.notes = append_note(candidate.notes, result.structured_result.get("additional_notes"))
    return bool(result.structured_result.get("confirmed", False))


def send_release(candidate: InterpreterCandidate, reason: str) -> None:
    """Tells an already-confirmed freelancer the appointment is off. Called
    from appointment.cancel_appointment() when the user cancels an
    already-succeeded booking outright. `batch_position=0`: this is always a
    single, standalone call, never one of a batch -- see calle/run.py's
    resolve_dial_target() for why a first-time recipient needs a position at
    all."""
    task = f"Call this interpreter and let them know the appointment was cancelled: {reason}."
    call_and_wait(
        task, candidate.phone, {"type": "object"},
        batch_position=0, purpose=CallPurpose.INTERPRETER_RELEASE,
    )
