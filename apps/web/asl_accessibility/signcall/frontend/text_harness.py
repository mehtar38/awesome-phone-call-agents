"""
The text-input testing harness described in the top-level README's
"Test the pipeline with typed text" section.

Every scenario builds a plain JSON object and runs it through
`workflow.user_input.load_user_input()`, so the committed schema
(schemas/user_input.schema.json) is exercised on every run rather than being
documentation nobody executes.

This stands in for two things that aren't built yet:

  1. Whatever eventually produces that JSON (a signing UI, a form, a stored
     profile). The workflow doesn't care which.
  2. A real CALL-E account. CALLE_MOCK_MODE=1 scripts every phone number's
     response instead of placing real calls, so the whole sequence (clinic
     batch search, family list, freelance batches, decline-and-retry, the
     already-have-an-interpreter path) can be exercised for free.

Every scenario asserts in-process, so a regression fails loudly instead of
printing a plausible-looking result. Mock resolvers branch on the call's
PURPOSE as well as the phone number, because the winning clinic's number is
called twice per run (search, then book) and an interpreter's up to twice
(availability, then binding confirm) -- the phone alone can't tell those apart.

Run from `apps/` (the parent of `signcall/`), NOT from inside
`apps/signcall/` itself:
    python3 -m signcall.frontend.text_harness clinic_search
Running from inside apps/signcall/ puts that directory on sys.path, which
makes `import calle` in calle/run.py resolve to THIS app's own calle/ adapter
folder instead of the real installed calle-ai SDK -- a real bug hit during
development. See README.md's naming-collision section.
"""

import json
import os
import random
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# Forced, not defaulted: signcall/.env may say CALLE_MOCK_MODE=0, and this
# harness must never be able to place a real call.
os.environ["CALLE_MOCK_MODE"] = "1"

from ..calle import run as calle_run  # noqa: E402
from ..calle.run import CallPurpose, CallResult, register_mock  # noqa: E402
from ..workflow import clinic_lookup, interpreter_lookup, interpreter_matching  # noqa: E402
from ..workflow.appointment import run_interpreter_mesh as _run_interpreter_mesh  # noqa: E402
from ..workflow.confirmation import BookingDeclined, BookingProposal  # noqa: E402
from ..workflow.types import AppointmentType, ClinicCandidate, InterpreterCandidate  # noqa: E402
from ..workflow.user_input import UserInputError, load_user_input  # noqa: E402

USER_PHONE = "+17025550999"
USER_ZIP = "89101"  # Las Vegas -- this build's data is Nevada-scoped

# Every call placed in a scenario, in order, as (phone, task, purpose).
# Scenarios assert against this to prove which calls were NOT placed -- which
# is where most of this design's behaviour lives.
CALLS: list[tuple[str, str, CallPurpose]] = []

# Every proposal the workflow put to the user, in order.
PROPOSALS: list[BookingProposal] = []


def _reset() -> None:
    CALLS.clear()
    PROPOSALS.clear()


def _force_parallel_batch(on: bool) -> bool:
    """PARALLEL_BATCH_CALLS defaults to OFF (sequential dispatch) in
    production -- see interpreter_matching's module docstring -- but
    scenario_freelance_parallel exists specifically to prove the PARALLEL
    dispatch path still works when it's turned on. It forces this here and
    restores it with _restore_parallel_batch() before returning. Not
    reentrant/thread-safe, but every scenario in this file runs in its own
    fresh process (see the module docstring's run instructions), so nothing
    here ever overlaps."""
    old = interpreter_matching.PARALLEL_BATCH_CALLS
    interpreter_matching.PARALLEL_BATCH_CALLS = on
    return old


def _restore_parallel_batch(old: bool) -> None:
    interpreter_matching.PARALLEL_BATCH_CALLS = old


def _is_booking(purpose: CallPurpose) -> bool:
    return purpose is CallPurpose.CLINIC_BOOK


def _is_confirm(purpose: CallPurpose) -> bool:
    """Any BINDING confirm -- family or freelance -- as opposed to the
    earlier, non-binding availability asks."""
    return purpose in (CallPurpose.INTERPRETER_CONFIRM, CallPurpose.FAMILY_CONFIRM)


def _phones_called() -> list[str]:
    return [phone for phone, _, _ in CALLS]


def _approve(proposal: BookingProposal) -> bool:
    """Says yes -- after checking the invariant every scenario relies on:
    nothing binding may have been called before the user was asked."""
    assert not any(_is_booking(p) or _is_confirm(p) for _, _, p in CALLS), (
        "BUG: a booking or interpreter confirmation happened before the user approved"
    )
    PROPOSALS.append(proposal)
    return True


def _decline(proposal: BookingProposal) -> bool:
    PROPOSALS.append(proposal)
    return False


def run_interpreter_mesh(user, clinics=None, candidates=None, *, approve=_approve):
    """The workflow, approved by default; scenarios testing a refusal pass
    `approve=_decline`."""
    return _run_interpreter_mesh(user, clinics, candidates, approve=approve)


def _ok(structured: dict) -> CallResult:
    return CallResult(
        status="completed", task_completed=True, completion_confidence=0.9,
        structured_result=structured,
    )


def _no_answer() -> CallResult:
    return CallResult(
        status="no_answer", task_completed=False, completion_confidence=0.0,
        structured_result={},
    )


def _date(day_offset: int) -> str:
    return (datetime.now() + timedelta(days=day_offset)).strftime("%Y-%m-%d")


def _slot(day_offset: int, time: str) -> dict:
    """One availability entry. The weekday name is derived from the date so
    the two always agree -- load_user_input rejects them when they don't."""
    when = datetime.now() + timedelta(days=day_offset)
    return {
        "day": when.strftime("%A").lower(),
        "date": when.strftime("%Y-%m-%d"),
        "time": time,
    }


_WEEKDAY_CYCLE = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _slot_with_wrong_weekday(day_offset: int, time: str) -> dict:
    """Same as _slot(), but `day` is deliberately wrong for `date` -- the next
    name in the weekday cycle, so it can never accidentally agree. A past
    version of this test hardcoded "sunday" here, which is only a mismatch
    on days when day_offset days from now ISN'T actually a Sunday -- a real,
    found bug (it fails every time that coincidence lines up, e.g. when
    today + 3 days really is a Sunday)."""
    slot = _slot(day_offset, time)
    wrong_index = (_WEEKDAY_CYCLE.index(slot["day"]) + 1) % 7
    return {**slot, "day": _WEEKDAY_CYCLE[wrong_index]}


def _input_json(**overrides) -> dict:
    base = {
        "name": "Dana Reyes",
        "date_of_birth": "1991-04-12",
        "age": 35,
        "phone_number": USER_PHONE,
        "zipcode": USER_ZIP,
        "appointment_type": "dental",
        "insurance": {"provider_name": "Silver State Health", "policy_number": "SSH-4471902"},
        "availability": [_slot(3, "2PM"), _slot(4, "10AM"), _slot(7, "3PM")],
        "has_interpreter": False,
        "family_members": [],
    }
    base.update(overrides)
    return base


# --- mock factories ------------------------------------------------------

def clinic(
    name: str,
    phone: str,
    distance: float,
    *,
    zipcode: str = USER_ZIP,
    accepts_insurance: bool = True,
    slots: tuple = (),
    answers: bool = True,
    booking_reference: str = "CLINIC-X",
    requirements: tuple = (),
    notes: str = "",
    blocked_reason: str | None = None,
    cancels: bool = True,
    offers_alternate: "tuple[str, str] | None" = None,
) -> ClinicCandidate:
    """Registers this clinic's scripted responses and returns the candidate
    object, so scenarios can assert on what the search wrote back into it.
    `blocked_reason` makes the BOOKING call refuse (booked False); `cancels`
    says whether a later CANCEL call succeeds. `offers_alternate=(date, time)`
    scripts a misbehaving booking call that reports booked=True for a
    DIFFERENT date/time than whatever was actually asked for -- a real,
    confirmed bug (see scenario_clinic_alternate_date_rejected)."""

    def resolver(task, phone_, purpose):
        CALLS.append((phone_, task, purpose))
        if purpose is CallPurpose.CLINIC_CANCEL:
            return _ok({"cancelled": cancels})
        if _is_booking(purpose):
            if blocked_reason:
                return _ok({
                    "booked": False, "blocked_reason": blocked_reason,
                    "requirements": list(requirements),
                })
            if offers_alternate:
                alt_date, alt_time = offers_alternate
                return _ok({
                    "booked": True, "confirmed_by": "Dana",
                    "confirmed_date": alt_date, "confirmed_time": alt_time,
                    "booking_reference": booking_reference,
                })
            return _ok({
                "booked": True, "confirmed_by": "Dana",
                "booking_reference": booking_reference,
            })
        if not answers:
            return _no_answer()
        if not accepts_insurance:
            # A clinic that says no is never asked about slots -- that branch
            # happens inside CALL-E's own conversation, so what's assertable
            # here is that no slots ever come back.
            return _ok({"accepts_insurance": False})
        return _ok({
            "accepts_insurance": True,
            "available_slots": [{"date": d, "time": t} for d, t in slots],
            "requirements": list(requirements),
            "additional_notes": notes,
        })

    register_mock(phone, resolver)
    return ClinicCandidate(
        name=name, phone=phone, zipcode=zipcode, clinic_type="Dentist",
        distance_miles=distance, source="injected",
    )


def family(
    name: str, relation: str, phone: str, *,
    covers: tuple = (), answers: bool = True, confirms: bool = True,
) -> dict:
    """`confirms` scripts the answer to the LATER binding confirm call (after
    booking), separately from `covers`/`answers`, which scripts the earlier
    availability call -- a member can say yes to availability and still
    decline the binding confirm, or never be asked availability at all and
    still be reached directly for a confirm (the fallback path)."""

    def resolver(task, phone_, purpose):
        CALLS.append((phone_, task, purpose))
        if not answers:
            return _no_answer()
        if purpose is CallPurpose.FAMILY_CONFIRM:
            return _ok({"confirmed": confirms})
        return _ok({"coverable_slots": list(covers)})

    register_mock(phone, resolver)
    return {"name": name, "relation": relation, "phone_number": phone}


def freelancer(
    name: str,
    phone: str,
    *,
    covers: tuple = (),
    rate: float | None = None,
    minimum_hours: float | None = 2.0,
    confirms: bool = True,
    answers: bool = True,
    register: bool = True,
    zipcode: str = USER_ZIP,
    distance: float = 2.0,
    wait_for: "threading.Barrier | None" = None,
) -> InterpreterCandidate:
    """`register=False` deliberately leaves this number unmocked, so any call
    to it raises KeyError loudly rather than failing silently. `wait_for` makes
    the availability call block on a barrier, to prove calls overlap."""

    def resolver(task, phone_, purpose):
        CALLS.append((phone_, task, purpose))
        if wait_for is not None and purpose is CallPurpose.INTERPRETER_AVAILABILITY:
            wait_for.wait()
        if not answers:
            return _no_answer()
        if _is_confirm(purpose):
            return _ok({"confirmed": confirms})
        return _ok({
            "coverable_slots": list(covers),
            "rate_per_hour": rate,
            "minimum_hours": minimum_hours,
        })

    if register:
        register_mock(phone, resolver)
    return InterpreterCandidate(
        name=name, phone=phone, zipcode=zipcode, city="Las Vegas",
        age=40, expertise=["medical"], distance_miles=distance, source="injected",
    )


# --- scenarios -----------------------------------------------------------

def scenario_clinic_search() -> None:
    """
    The finalized workflow end-to-end: an insurance-gated clinic search that
    calls clinics one at a time, nearest first, and stops at the first match;
    then family, then a freelance batch that tie-breaks by rate; then the user
    is asked, the callback booking (the only call that identifies the patient)
    is placed, and both confirmation texts go out.

    The nearest three clinics fail three different ways -- declines insurance /
    accepts but no overlapping slot / doesn't answer -- and a fourth declines
    too, so the search moves on. The next one matches and wins. Nothing after
    it is called, including a farther clinic that would also have matched.
    """
    _reset()
    thu, fri, mon = _date(3), _date(4), _date(7)

    no_insurance = clinic("A (no insurance)", "+17025551001", 1.0, accepts_insurance=False)
    no_overlap = clinic("B (early mornings)", "+17025551002", 2.0,
                        slots=((thu, "08:00"), (fri, "08:30")))
    silent = clinic("E (no answer)", "+17025551003", 3.0, answers=False)
    also_no_insurance = clinic("F (no insurance)", "+17025551004", 4.0, accepts_insurance=False)
    nearer_match = clinic("D (match, nearer)", "+17025551005", 4.5, zipcode="89106",
                          slots=((thu, "14:00"), (fri, "10:00"), (mon, "15:00")),
                          booking_reference="CLINIC-D")
    farther_match = clinic("C (match, farther)", "+17025551006", 5.0,
                           slots=((thu, "14:00"),), booking_reference="CLINIC-C")
    never_called = clinic("G (after the match)", "+17025551007", 6.0, slots=((thu, "14:00"),))

    sister = family("Maya Reyes", "sister", "+17025552001", covers=())

    pricier = freelancer("R. Alvarez", "+17025553001", covers=(f"{thu} 14:00",), rate=110.0)
    cheaper = freelancer("J. Kim", "+17025553002", covers=(f"{thu} 14:00",), rate=95.0)
    unavailable = freelancer("T. Osei", "+17025553003", covers=())

    user = load_user_input(_input_json(family_members=[sister]))
    result = run_interpreter_mesh(
        user,
        clinics=[farther_match, silent, no_insurance, never_called,
                 nearer_match, no_overlap, also_no_insurance],
        candidates=[pricier, cheaper, unavailable],
    )
    print("\n--- RESULT ---")
    print(result)

    assert _phones_called().count(no_insurance.phone) == 1, "A should be called exactly once"
    assert no_insurance.offered_slots == [], "A declined insurance -- no slots should come back"
    assert silent.accepts_insurance is None, (
        "A no-answer must stay distinguishable from a policy decline"
    )
    assert never_called.phone not in _phones_called(), (
        "BUG: the search kept calling after a clinic matched"
    )
    assert farther_match.phone not in _phones_called(), (
        "BUG: a farther clinic was called after a nearer one had already matched"
    )
    assert result.appointment["booking_reference"] == "CLINIC-D", (
        "BUG: the nearest matching clinic wasn't chosen"
    )
    clinic_calls = [p for _, _, p in CALLS if p is CallPurpose.CLINIC_SEARCH]
    assert len(clinic_calls) == 5, (
        "exactly five clinics should have been called: the four that failed and the match"
    )
    assert result.interpreter["name"] == "J. Kim", (
        f"BUG: cheapest-by-rate should win, got {result.interpreter['name']!r}"
    )
    assert result.family_tier_result == "no_overlap"

    book_idx = next(i for i, (_, _, p) in enumerate(CALLS) if _is_booking(p))
    confirm_idx = next(i for i, (_, _, p) in enumerate(CALLS) if _is_confirm(p))
    assert book_idx < confirm_idx, (
        "The clinic is booked BEFORE the fee-bearing interpreter is asked to commit"
    )
    assert len(PROPOSALS) == 1, "the user is asked exactly once"
    proposal = PROPOSALS[0]
    assert proposal.clinic_name == "D (match, nearer)"
    assert (proposal.date, proposal.time) == (thu, "14:00")
    assert proposal.interpreter["name"] == "J. Kim"
    assert [a["name"] for a in proposal.alternates] == ["R. Alvarez"], (
        "the rest of the matching batch who named the same time are the fallbacks"
    )
    booking_task = CALLS[book_idx][1]
    for expected in ("Dana Reyes", "1991-04-12", "Silver State Health", "SSH-4471902"):
        assert expected in booking_task, f"Booking call must give the clinic {expected!r}"
    clinic_search_tasks = [t for _, t, p in CALLS if p is CallPurpose.CLINIC_SEARCH]
    interpreter_tasks = [t for _, t, p in CALLS if p is CallPurpose.INTERPRETER_AVAILABILITY]
    family_tasks = [t for _, t, p in CALLS if p is CallPurpose.FAMILY_AVAILABILITY]
    assert all("Dana Reyes" not in t for t in clinic_search_tasks + interpreter_tasks), (
        "BUG: a clinic-search or interpreter call leaked the patient's name -- "
        "only the booking call and the family call use it."
    )
    assert all(USER_PHONE not in t for t in interpreter_tasks), (
        "BUG: the non-binding availability call gave out the patient's phone number"
    )
    assert interpreter_tasks and all("D (match, nearer)" in t for t in interpreter_tasks), (
        "the availability ask must say which clinic the job is at, so an "
        "interpreter can judge travel before quoting a rate"
    )
    confirm_task = next(t for _, t, p in CALLS if p is CallPurpose.INTERPRETER_CONFIRM)
    for expected in ("Dana Reyes", USER_PHONE, "D (match, nearer)"):
        assert expected in confirm_task, (
            f"the binding confirm call -- made only to whoever is actually "
            f"hired -- must give {expected!r}"
        )
    assert all(
        "SSH-4471902" not in t and "1991-04-12" not in t for t in clinic_search_tasks
    ), "BUG: a clinic-search call gave out the policy number or date of birth"
    for task in clinic_search_tasks:
        assert "Silver State Health" in task, "search call must be able to say the insurance provider"
        assert "between 2:00 PM and 3:00 PM" in task, "search call must state the user's free times"
        assert "YYYY-MM-DD" in task and "24-hour" in task, "search call must state the slot format"
        assert "do not book" in task, "a search call must tell the agent not to book"
    assert family_tasks and all("Dana Reyes" in t for t in family_tasks), (
        "the family call must say who it is calling on behalf of"
    )
    assert len([e for e in result.evidence if "Confirmation text" in e]) == 2
    print("\nOK: insurance gate, one-at-a-time nearest-first search that stops at "
          "the first match, cheapest-by-rate, user asked once before anything binding, clinic "
          "booked before the interpreter commits, patient details on the "
          "booking call only, and both confirmation texts.")


def scenario_shortcut() -> None:
    """has_interpreter: true. Step 3 is trivially satisfied by Step 2's clinic
    match; the same insurance-gated search still runs. No contact details are
    collected for the user's own interpreter, so nobody is called or texted
    about them and only the user gets a confirmation text.

    The slot is chosen at random among the matched ones, so this seeds RNG for
    reproducible demo takes and asserts membership, not equality."""
    _reset()
    random.seed(0)
    thu, fri, mon = _date(3), _date(4), _date(7)

    declines = clinic("A (no insurance)", "+17025551001", 1.0, accepts_insurance=False)
    match = clinic("B (match)", "+17025551002", 2.0,
                   slots=((thu, "14:00"), (fri, "10:00"), (mon, "15:00")),
                   booking_reference="CLINIC-SHORTCUT")

    user = load_user_input(_input_json(has_interpreter=True, family_members=[]))
    result = run_interpreter_mesh(user, clinics=[match, declines])
    print("\n--- RESULT ---")
    print(result)

    assert result.interpreter_source == "user_arranged"
    assert result.interpreter == {"tier": "user_arranged"}, (
        "No name or number exists for a user-arranged interpreter"
    )
    assert declines.offered_slots == [], "insurance gate applies on this path too"
    texts = [e for e in result.evidence if "Confirmation text" in e]
    assert len(texts) == 1 and "user" in texts[0], (
        f"Only the user can be texted on this path, got {texts}"
    )
    booked_task = next(t for _, t, p in CALLS if _is_booking(p))
    assert any(f"Book the {d} {t} appointment" in booked_task
               for d, t in ((thu, "14:00"), (fri, "10:00"), (mon, "15:00"))), (
        f"Booked slot must be one of the matched slots; task was {booked_task!r}"
    )
    print("\nOK: same clinic search ran, one of the matched slots was booked, "
          "and only the user was notified.")


def scenario_decline_then_retry() -> None:
    """The cheapest-by-rate interpreter in the matching batch declines the
    binding confirm, and the agent falls through to the next match IN THAT SAME
    BATCH -- without calling any further batch, and without booking the clinic
    a second time (the fallback named the same slot)."""
    _reset()
    thu = _date(3)
    matched = clinic("Clinic", "+17025551001", 1.0, slots=((thu, "14:00"),),
                     booking_reference="CLINIC-DECLINE")

    declines = freelancer("A (cheaper, declines)", "+17025553001",
                          covers=(f"{thu} 14:00",), rate=90.0, confirms=False)
    confirms = freelancer("B (pricier, confirms)", "+17025553002",
                          covers=(f"{thu} 14:00",), rate=130.0)
    third = freelancer("C (unavailable)", "+17025553003", covers=())
    next_batch = freelancer("D (batch 2)", "+17025553004", covers=(f"{thu} 14:00",),
                            rate=50.0, register=False)

    result = run_interpreter_mesh(
        load_user_input(_input_json()),
        clinics=[matched],
        candidates=[declines, confirms, third, next_batch],
    )
    print("\n--- RESULT ---")
    print(result)

    assert result.interpreter["name"] == "B (pricier, confirms)"
    assert result.appointment["booking_reference"] == "CLINIC-DECLINE"
    assert sum(1 for _, _, p in CALLS if _is_booking(p)) == 1
    assert next_batch.phone not in _phones_called(), (
        "BUG: a second batch was called -- the search stops at the first batch "
        "with a match, even if someone in it declines."
    )
    assert sum(1 for _, _, p in CALLS if _is_confirm(p)) == 2, "both batch mates were asked"
    print("\nOK: the decline was absorbed within the matching batch, no further "
          "batch was called, and exactly one booking call was made.")


def scenario_cheapest_wrong_day() -> None:
    """
    Regression test for a real, confirmed bug: the cheapest responding
    candidate could only cover Monday, but the old code booked and confirmed
    her for Thursday, because coverable_slots was read as a truthiness check
    and discarded. The cheapest-by-rate candidate still wins -- but the slot
    is derived from HER OWN answer, so the correct outcome is Monday.
    """
    _reset()
    thu, mon = _date(3), _date(7)
    matched = clinic("Clinic", "+17025551001", 1.0,
                     slots=((thu, "14:00"), (mon, "15:00")),
                     booking_reference="CLINIC-SLOTMATCH")

    cheap_monday = freelancer("A (cheap, Monday-only)", "+17025553001",
                              covers=(f"{mon} 15:00",), rate=80.0)
    pricier_thursday = freelancer("B (pricier, Thursday)", "+17025553002",
                                  covers=(f"{thu} 14:00",), rate=130.0)

    result = run_interpreter_mesh(
        load_user_input(_input_json()),
        clinics=[matched],
        candidates=[cheap_monday, pricier_thursday],
    )
    print("\n--- RESULT ---")
    print(result)

    assert result.interpreter["name"] == "A (cheap, Monday-only)"
    booked_task = next(t for _, t, p in CALLS if _is_booking(p))
    assert f"Book the {mon} 15:00 appointment" in booked_task, (
        f"BUG: booked a slot the confirmed interpreter never named -- {booked_task!r}"
    )
    print("\nOK: the cheapest candidate was confirmed for the slot SHE named, "
          "and the clinic was booked for that same slot.")


def scenario_family_locks_in() -> None:
    """
    Family is a definite, ordered, call-only list checked AFTER the clinic
    match. The first member doesn't answer; the second can cover a matched
    slot and is locked in, and nobody further is called during the search.
    Once the clinic is booked, that same member gets a binding confirm call
    (Step 5) before anyone is texted -- their earlier "yes" was to a slot that
    wasn't booked yet.

    Freelance candidates ARE passed but deliberately left unmocked -- if
    family were skipped, or its confirm skipped or declined, the run would
    try to call them and raise KeyError loudly instead of quietly succeeding
    another way.
    """
    _reset()
    thu, mon = _date(3), _date(7)
    matched = clinic("Clinic", "+17025551001", 1.0,
                     slots=((thu, "14:00"), (mon, "15:00")),
                     booking_reference="CLINIC-FAMILY")

    unreachable = family("Aaron Reyes", "brother", "+17025552001", answers=False)
    # Covers the SECOND matched slot, proving they're locked in for the slot
    # they actually named rather than the first one on the list.
    sister = family("Maya Reyes", "sister", "+17025552002", covers=(f"{mon} 15:00",))
    cousin = family("Nia Reyes", "cousin", "+17025552003", covers=(f"{thu} 14:00",))
    never = freelancer("Freelancer", "+17025553001", covers=(f"{thu} 14:00",),
                       rate=50.0, register=False)

    result = run_interpreter_mesh(
        load_user_input(_input_json(family_members=[unreachable, sister, cousin])),
        clinics=[matched],
        candidates=[never],
    )
    print("\n--- RESULT ---")
    print(result)

    assert result.family_tier_result == "locked_in"
    assert result.interpreter_source == "family"
    assert result.interpreter["name"] == "Maya Reyes"
    assert result.interpreter["relation"] == "sister"
    assert unreachable["phone_number"] in _phones_called(), "list order: brother called first"
    assert cousin["phone_number"] not in _phones_called(), (
        "BUG: kept calling the family list after a member said yes"
    )
    booked_task = next(t for _, t, p in CALLS if _is_booking(p))
    assert f"Book the {mon} 15:00 appointment" in booked_task
    confirm_calls = [(phone, purpose) for phone, _, purpose in CALLS if purpose is CallPurpose.FAMILY_CONFIRM]
    assert confirm_calls == [(sister["phone_number"], CallPurpose.FAMILY_CONFIRM)], (
        f"BUG: expected exactly one family confirm call, to Maya -- got {confirm_calls}"
    )
    book_idx = next(i for i, (_, _, p) in enumerate(CALLS) if _is_booking(p))
    confirm_idx = next(i for i, (_, _, p) in enumerate(CALLS) if p is CallPurpose.FAMILY_CONFIRM)
    assert book_idx < confirm_idx, (
        "the clinic must be booked BEFORE the family member is asked to commit"
    )
    texts = [e for e in result.evidence if "Confirmation text" in e]
    assert len(texts) == 2 and any("Maya Reyes" in t for t in texts), (
        f"The secured family member is texted the outcome too, got {texts}"
    )
    print("\nOK: family called one at a time in list order after the clinic "
          "match, stopped at the first yes, confirmed for real after booking, "
          "no freelance call placed, and the secured family member was texted.")


def scenario_family_confirm_fallback() -> None:
    """
    The binding family confirm (Step 5) can fail, and three things can happen
    next, each its own phase below:

      A. The locked-in member declines; the next member on the list, never
         asked during the search, is reached directly and confirms. Source
         stays "family".
      B. Every family member declines; a freelance search for this one
         already-booked slot finds someone who confirms. Source flips to
         "freelance" -- exactly the tier that would have run if no family
         member had matched in the first place.
      C. Nobody -- family or freelance -- confirms, so the clinic booking is
         cancelled.
    """
    _reset()
    thu = _date(3)

    # --- A: locked-in member declines, backup (never called) confirms ------
    matched = clinic("Clinic", "+17025551061", 1.0, slots=((thu, "14:00"),),
                     booking_reference="CLINIC-FAMFALLBACK-A")
    primary = family("Primary", "sister", "+17025552061",
                     covers=(f"{thu} 14:00",), confirms=False)
    backup = family("Backup", "cousin", "+17025552062", confirms=True)
    never = freelancer("Freelancer", "+17025553061", covers=(f"{thu} 14:00",),
                       rate=50.0, register=False)

    result = run_interpreter_mesh(
        load_user_input(_input_json(family_members=[primary, backup])),
        clinics=[matched], candidates=[never],
    )
    print("\n--- RESULT (A) ---")
    print(result)

    assert result.interpreter_source == "family"
    assert result.interpreter["name"] == "Backup"
    confirm_order = [phone for phone, _, p in CALLS if p is CallPurpose.FAMILY_CONFIRM]
    assert confirm_order == [primary["phone_number"], backup["phone_number"]], (
        f"BUG: expected the primary confirm to be tried before the backup, got {confirm_order}"
    )
    assert never.phone not in _phones_called(), (
        "BUG: freelance was called even though a family fallback confirmed"
    )
    texts = [e for e in result.evidence if "Confirmation text" in e]
    assert any("Backup" in t for t in texts), f"the member who actually confirmed is texted, got {texts}"

    # --- B: every family member declines, freelance covers the exact slot --
    _reset()
    matched_b = clinic("Clinic B", "+17025551071", 1.0, slots=((thu, "14:00"),),
                       booking_reference="CLINIC-FAMFALLBACK-B")
    primary_b = family("Primary B", "sister", "+17025552071",
                       covers=(f"{thu} 14:00",), confirms=False)
    kim = freelancer("J. Kim", "+17025553071", covers=(f"{thu} 14:00",), rate=95.0)

    result_b = run_interpreter_mesh(
        load_user_input(_input_json(family_members=[primary_b])),
        clinics=[matched_b], candidates=[kim],
    )
    print("\n--- RESULT (B) ---")
    print(result_b)

    assert result_b.interpreter_source == "freelance", (
        "BUG: source must flip to freelance once every family member declines"
    )
    assert result_b.interpreter["name"] == "J. Kim"
    assert result_b.family_tier_result == "locked_in", (
        "family_tier_result describes the SEARCH tier's outcome, not who "
        "ultimately confirmed -- it must not be rewritten by the fallback"
    )
    assert sum(1 for _, _, p in CALLS if p is CallPurpose.CLINIC_BOOK) == 1, (
        "the clinic must still be booked only once, not re-booked for the fallback"
    )

    # --- C: nobody confirms, the booking is cancelled -----------------------
    _reset()
    undoable = clinic("Clinic C", "+17025551081", 1.0, slots=((thu, "14:00"),),
                      booking_reference="CLINIC-FAMFALLBACK-C")
    primary_c = family("Primary C", "sister", "+17025552081",
                       covers=(f"{thu} 14:00",), confirms=False)
    declines = freelancer("Declines", "+17025553081", covers=(f"{thu} 14:00",),
                          rate=80.0, confirms=False)
    try:
        run_interpreter_mesh(
            load_user_input(_input_json(family_members=[primary_c])),
            clinics=[undoable], candidates=[declines],
        )
    except RuntimeError as exc:
        message = str(exc)
        print(f"\n--- RAISED (expected, C) ---\n{message}")
    else:
        raise AssertionError("BUG: nobody confirmed, yet the run succeeded")

    assert "family member and freelance interpreter" in message
    assert "was cancelled" in message
    assert [p for _, _, p in CALLS][-1] is CallPurpose.CLINIC_CANCEL, (
        "the booking must be undone last"
    )

    print("\nOK: a declined family confirm fell back to the next family "
          "member, then to freelance for the exact booked slot, and to "
          "cancellation when nobody confirmed at all.")


def scenario_clinic_search_exhausted() -> None:
    """The 10-clinic ceiling: 11 clinics are offered, none of the nearest 10
    match, and the 11th (which would have matched) must never be called."""
    _reset()
    thu = _date(3)
    clinics = [
        clinic(f"Clinic {i}", f"+1702555{2000 + i:04d}", float(i),
               accepts_insurance=(i % 2 == 0), slots=((thu, "08:00"),))
        for i in range(10)
    ]
    would_match = clinic("Eleventh", "+17025559999", 99.0, slots=((thu, "14:00"),))

    try:
        run_interpreter_mesh(load_user_input(_input_json()),
                             clinics=clinics + [would_match], candidates=[])
    except RuntimeError as exc:
        print(f"\n--- RAISED (expected) ---\n{exc}")
    else:
        raise AssertionError("BUG: should have raised -- no clinic matched")

    assert len(CALLS) == 10, f"Expected exactly 10 clinic calls, got {len(CALLS)}"
    assert not PROPOSALS, "the user is only asked once there is something to approve"
    assert would_match.phone not in _phones_called(), "BUG: searched past the ceiling"
    print("\nOK: exactly 10 clinics called, one at a time, ceiling "
          "respected, nothing booked.")


def scenario_requirements() -> None:
    """What a clinic says is required (a referral, ID) is passed on to the
    user without changing which clinic is booked. It only stops the run when
    the clinic refuses to book because of it."""
    _reset()
    thu = _date(3)

    needs_referral = clinic(
        "Clinic", "+17025551001", 1.0, slots=((thu, "14:00"),),
        requirements=("Referral from a primary care doctor", "Photo ID"),
        notes="Front desk closes at 5pm", booking_reference="CLINIC-REQ",
    )
    kim = freelancer("J. Kim", "+17025553002", covers=(f"{thu} 14:00",), rate=95.0)
    result = run_interpreter_mesh(
        load_user_input(_input_json()), clinics=[needs_referral], candidates=[kim]
    )
    print("\n--- RESULT ---")
    print(result)

    assert result.appointment["booking_reference"] == "CLINIC-REQ", (
        "BUG: a requirement blocked a booking the clinic was willing to make"
    )
    assert result.requirements == ["Referral from a primary care doctor", "Photo ID"]
    assert any("Front desk closes at 5pm" in note for note in result.notes)
    booked_task = next(t for _, t, p in CALLS if _is_booking(p))
    assert "Referral from a primary care doctor" in booked_task, (
        "the booking call must know what the clinic already said was required"
    )
    assert "has been told about them and wants to proceed" in booked_task, (
        "the user saw these requirements before approving, and the clinic is told so"
    )
    assert PROPOSALS[0].requirements == ["Referral from a primary care doctor", "Photo ID"], (
        "the user must see the requirements BEFORE deciding"
    )
    assert any("Front desk closes at 5pm" in n for n in PROPOSALS[0].notes)
    user_text = next(e for e in result.evidence if "to user" in e)
    interpreter_text = next(e for e in result.evidence if "to interpreter" in e)
    assert "Required for the appointment" in user_text
    assert "Required for the appointment" not in interpreter_text, (
        "the interpreter doesn't need the patient's paperwork"
    )

    _reset()
    strict = clinic(
        "Strict clinic", "+17025551011", 1.0, slots=((thu, "14:00"),),
        requirements=("Referral from a primary care doctor",),
        blocked_reason="We can't book without a referral on file.",
    )
    lee = freelancer("J. Lee", "+17025553012", covers=(f"{thu} 14:00",), rate=95.0)
    try:
        run_interpreter_mesh(
            load_user_input(_input_json()), clinics=[strict], candidates=[lee]
        )
    except RuntimeError as exc:
        message = str(exc)
        print(f"\n--- RAISED (expected) ---\n{message}")
    else:
        raise AssertionError("BUG: a clinic that refused to book was treated as booked")
    assert "can't book without a referral" in message, "the clinic's own reason must reach the user"
    assert "Referral from a primary care doctor" in message
    assert "no interpreter was confirmed" in message
    assert not any(_is_confirm(p) for _, _, p in CALLS), (
        "BUG: an interpreter was asked to commit for an appointment that was never booked"
    )

    print("\nOK: requirements and notes reached the user without blocking the "
          "booking, and a clinic that refused to book stopped the run with its "
          "own reason.")


def scenario_clinic_alternate_date_rejected() -> None:
    """Regression test for a real, confirmed bug: the user approved
    2026-09-30, the clinic offered 2026-10-01 instead, and the calling agent
    accepted it and reported booked=True. Nothing checked WHICH date was
    actually confirmed, so the run went on to tell the interpreter and the
    user about 2026-09-30 -- a date that was never actually booked.

    A booking call that reports a different confirmed_date/confirmed_time
    than what was asked for must be treated exactly like a refusal: the run
    ends there, and nothing after book_slot() -- no interpreter confirm, no
    confirmation texts -- may ever run."""
    _reset()
    approved = _date(3)       # what the user actually approved -- 14:00 overlaps
                              # the default input's free window on this day
    offered = _date(4)        # what the clinic offers instead, mid-booking-call
    switcheroo = clinic("FYZICAL Therapy", "+17025551091", 1.0,
                        slots=((approved, "14:00"),),
                        offers_alternate=(offered, "14:00"))
    kim = freelancer("J. Kim", "+17025553091", covers=(f"{approved} 14:00",), rate=95.0)

    try:
        run_interpreter_mesh(
            load_user_input(_input_json()), clinics=[switcheroo], candidates=[kim]
        )
    except RuntimeError as exc:
        message = str(exc)
        print(f"\n--- RAISED (expected) ---\n{message}")
    else:
        raise AssertionError(
            "BUG: the clinic booked a different date than the user approved, "
            "and the run treated it as a success"
        )

    assert "REJECTED" in message
    assert approved in message and offered in message, (
        f"the user must be told BOTH the date they approved and the date the "
        f"clinic actually booked, got: {message}"
    )
    assert not any(_is_confirm(p) for _, _, p in CALLS), (
        "BUG: an interpreter was confirmed for a date that was never actually booked"
    )
    assert not any(p is CallPurpose.CLINIC_CANCEL for _, _, p in CALLS), (
        "nothing needs cancelling on OUR side -- book_slot() never returned success"
    )
    assert len(PROPOSALS) == 1, "the user was asked once, over the ORIGINAL approved slot"
    print("\nOK: a clinic that booked a different date than the one approved "
          "was rejected outright -- the run ended there, and no interpreter "
          "was ever asked to confirm a date that was never actually booked.")


def scenario_freelance_parallel() -> None:
    """Proves the PARALLEL dispatch mode still works, even though
    PARALLEL_BATCH_CALLS defaults to OFF today -- a shared/free CALL-E line
    only places one call at a time on the account's behalf, so dialling a
    batch at once gains nothing until a dedicated number is bought. This
    scenario temporarily forces it on to prove the capability is there for
    when that happens, without changing the production default. The batch
    SIZE (2) is unaffected either way -- see interpreter_matching's module
    docstring for why size and dispatch are two different knobs.

    Each availability call waits at a barrier that only opens once both are
    in flight, so a sequential dispatch would leave the barrier waiting and
    fail here. The cheapest is last in the batch and the rates are out of
    order, so the ranking can't be an accident of which call happened to
    finish first."""
    _reset()
    real_parallel = _force_parallel_batch(True)
    try:
        thu = _date(3)
        barrier = threading.Barrier(2, timeout=5)
        a = freelancer("A", "+17025553051", covers=(f"{thu} 14:00",), rate=120.0, wait_for=barrier)
        b = freelancer("B", "+17025553052", covers=(f"{thu} 14:00",), rate=80.0, wait_for=barrier)
        next_batch = freelancer("C", "+17025553053", covers=(f"{thu} 14:00",), rate=10.0,
                                register=False)
        matched = clinic("Clinic", "+17025551051", 1.0, slots=((thu, "14:00"),),
                         booking_reference="CLINIC-PAR")

        result = run_interpreter_mesh(
            load_user_input(_input_json()), clinics=[matched], candidates=[a, b, next_batch]
        )
        print("\n--- RESULT ---")
        print(result)

        assert not barrier.broken, "both calls never overlapped"
        assert result.interpreter["name"] == "B", "cheapest of the batch wins, whatever finished first"
        assert PROPOSALS[0].interpreter["name"] == "B"
        assert [alt["name"] for alt in PROPOSALS[0].alternates] == ["A"], (
            "fallbacks are ordered cheapest-first regardless of arrival order"
        )
        assert next_batch.phone not in _phones_called(), "a match in the first batch ends the search"
    finally:
        _restore_parallel_batch(real_parallel)
    print("\nOK: with parallel dispatch forced on for this test, both "
          "freelancers were in flight at once, the cheapest won regardless "
          "of arrival order, and no second batch was called. (Production "
          "default stays sequential -- see SIGNCALL_FREELANCE_PARALLEL_BATCH.)")


def scenario_sequential_batch_calls_both() -> None:
    """Regression test for the exact misunderstanding this design almost
    shipped with: sequential dispatch must still call EVERY member of the
    batch and still pick the cheapest of them -- it must NOT stop as soon as
    the first one says yes. Both freelancers here are available; the one
    dialled FIRST is the pricier one, so if the search stopped at the first
    yes (wrong), the pricier one would win. Cheapest-wins here proves the
    second one was actually called and compared, not skipped."""
    assert not interpreter_matching.PARALLEL_BATCH_CALLS, (
        "this scenario asserts call ORDER, which only means something under "
        "sequential dispatch -- it isn't meaningful (or reliable) in parallel mode"
    )
    _reset()
    thu = _date(3)
    pricier_first = freelancer("A (pricier, dialled first)", "+17025553061",
                               covers=(f"{thu} 14:00",), rate=130.0)
    cheaper_second = freelancer("B (cheaper, dialled second)", "+17025553062",
                                covers=(f"{thu} 14:00",), rate=80.0)
    matched = clinic("Clinic", "+17025551061", 1.0, slots=((thu, "14:00"),),
                     booking_reference="CLINIC-SEQ")

    result = run_interpreter_mesh(
        load_user_input(_input_json()), clinics=[matched],
        candidates=[pricier_first, cheaper_second],
    )
    print("\n--- RESULT ---")
    print(result)

    availability_calls = [phone for phone, _, p in CALLS if p is CallPurpose.INTERPRETER_AVAILABILITY]
    assert availability_calls == [pricier_first.phone, cheaper_second.phone], (
        f"BUG: expected both freelancers dialled in order, got {availability_calls}"
    )
    assert result.interpreter["name"] == "B (cheaper, dialled second)", (
        "BUG: the search stopped at the first available freelancer instead "
        "of calling the whole batch and picking the cheapest -- this is "
        "exactly the regression this scenario exists to catch"
    )
    print("\nOK: sequential dispatch still called BOTH batch members -- the "
          "first one saying yes did not skip the second -- and the cheaper "
          "one, called second, still won.")


def scenario_declined() -> None:
    """If the user says no, the run ends right there. Everything before the
    proposal was a call that commits nobody; nothing after it is placed."""
    _reset()
    thu = _date(3)
    matched = clinic("Clinic", "+17025551021", 1.0, slots=((thu, "14:00"),))
    kim = freelancer("J. Kim", "+17025553022", covers=(f"{thu} 14:00",), rate=95.0)

    try:
        run_interpreter_mesh(
            load_user_input(_input_json()), clinics=[matched], candidates=[kim],
            approve=_decline,
        )
    except BookingDeclined as exc:
        print(f"\n--- RAISED (expected) ---\n{exc}")
    else:
        raise AssertionError("BUG: the user said no and the run carried on")

    assert len(PROPOSALS) == 1
    assert [p for _, _, p in CALLS] == [
        CallPurpose.CLINIC_SEARCH, CallPurpose.INTERPRETER_AVAILABILITY,
    ], "after a no, no booking, confirmation or cancellation call may be placed"
    print("\nOK: the user was asked once, said no, and only the two non-binding "
          "search calls had been placed.")


def scenario_interpreters_all_decline() -> None:
    """The clinic is booked first, so when every interpreter then declines the
    final confirmation, that booking has to be cancelled -- and if the clinic
    won't cancel, the user has to be told to."""
    _reset()
    thu = _date(3)
    undoable = clinic("Clinic", "+17025551031", 1.0, slots=((thu, "14:00"),),
                      booking_reference="CLINIC-UNDO")
    a = freelancer("A", "+17025553031", covers=(f"{thu} 14:00",), rate=80.0, confirms=False)
    b = freelancer("B", "+17025553032", covers=(f"{thu} 14:00",), rate=90.0, confirms=False)
    try:
        run_interpreter_mesh(
            load_user_input(_input_json()), clinics=[undoable], candidates=[a, b]
        )
    except RuntimeError as exc:
        message = str(exc)
        print(f"\n--- RAISED (expected) ---\n{message}")
    else:
        raise AssertionError("BUG: no interpreter confirmed, yet the run succeeded")

    purposes = [p for _, _, p in CALLS]
    assert purposes.count(CallPurpose.CLINIC_BOOK) == 1
    assert purposes.count(CallPurpose.INTERPRETER_CONFIRM) == 2, "both batch mates were asked"
    assert purposes[-1] is CallPurpose.CLINIC_CANCEL, "the booking must be undone last"
    cancel_task = CALLS[-1][1]
    assert "CLINIC-UNDO" in cancel_task and "Dana Reyes" in cancel_task
    assert "was cancelled" in message

    _reset()
    stuck = clinic("Stubborn clinic", "+17025551041", 1.0, slots=((thu, "14:00"),),
                   booking_reference="CLINIC-STUCK", cancels=False)
    c = freelancer("C", "+17025553041", covers=(f"{thu} 14:00",), rate=80.0, confirms=False)
    try:
        run_interpreter_mesh(
            load_user_input(_input_json()), clinics=[stuck], candidates=[c]
        )
    except RuntimeError as exc:
        message = str(exc)
        print(f"\n--- RAISED (expected) ---\n{message}")
    else:
        raise AssertionError("BUG: no interpreter confirmed, yet the run succeeded")
    assert "could NOT be cancelled" in message and "CLINIC-STUCK" in message, (
        "if the clinic won't cancel, the user must be told which booking to undo"
    )

    print("\nOK: after every interpreter declined, the booking was cancelled, "
          "and a failed cancellation told the user which booking to undo.")


def scenario_input_validation() -> None:
    """Everything that must be rejected BEFORE a single call is placed. Each
    of these would otherwise surface several real, paid calls deep."""
    _reset()
    cases = [
        ("weekday/date mismatch",
         _input_json(availability=[_slot_with_wrong_weekday(3, "2PM")]),
         "is a"),
        ("availability date in the past",
         _input_json(availability=[_slot(-2, "2PM")]),
         "in the past"),
        ("family listed when the user has their own interpreter",
         _input_json(has_interpreter=True,
                     family_members=[{"name": "Maya", "relation": "sister",
                                      "phone_number": "+17025552001"}]),
         "schema validation"),
        ("malformed time",
         _input_json(availability=[{**_slot(3, "2PM"), "time": "25PM"}]),
         "schema validation"),
        ("ZIP outside this build's covered states",
         _input_json(zipcode="10001"),  # New York -- neither NV nor IL
         "outside this build's coverage"),
        ("unknown appointment type",
         _input_json(appointment_type="podiatry"),
         "schema validation"),
        ("missing insurance policy number",
         _input_json(insurance={"provider_name": "Silver State Health"}),
         "schema validation"),
    ]
    for label, payload, expected_fragment in cases:
        try:
            load_user_input(payload)
        except UserInputError as exc:
            assert expected_fragment in str(exc), (
                f"{label}: rejected, but not for the stated reason -- {exc}"
            )
            print(f"  rejected ({label}): {str(exc)[:110]}...")
        else:
            raise AssertionError(f"BUG: {label} was accepted")

    good = load_user_input(_input_json())
    assert len(good.free_windows) == 3
    first = good.free_windows[0]
    assert (first.start.hour, first.end.hour) == (14, 15), (
        f"'2PM' must mean 14:00-15:00, got {first.start}-{first.end}"
    )
    assert good.free_windows[1].start.hour == 10, "'10AM' must mean 10:00-11:00"
    assert not CALLS, "validation must never place a call"
    print("\nOK: every malformed input was rejected before any call, and a good "
          "one parsed to one-hour windows.")


def scenario_clinic_lookup() -> None:
    """The Apify Google Maps parser, against canned dataset items -- no
    network. Covers the field mapping, every reason a place gets dropped, the
    four-field record schema, phone-based de-duplication, None-distance
    sorting, the inspection dump file, and the synthetic fallback."""
    _reset()
    items = [
        {  # complete listing: phoneUnformatted preferred, ZIP+4 trimmed
            "title": "Fremont Street Dental Care",
            "categoryName": "Dentist",
            "phoneUnformatted": "+17025550143",
            "phone": "(702) 555-0143",
            "postalCode": "89101-2231",
            "countryCode": "US",
        },
        {  # only a formatted phone -- must still normalize
            "title": "Sunridge Family Dentistry",
            "categoryName": "Dental clinic",
            "phone": "(775) 555-0177",
            "postalCode": "89509",
        },
        {  # no category at all -> falls back to the searched label
            "title": "Copper Ridge Dental",
            "phoneUnformatted": "7025550188",
            "postalCode": "89117",
        },
        {  # duplicate of the first listing's number (multi-location practice)
            "title": "Fremont Street Dental Care - Suite B",
            "categoryName": "Dentist",
            "phoneUnformatted": "+17025550143",
            "postalCode": "89106",
        },
        {  # off-category: Maps returns adjacent businesses
            "title": "Vegas Smile Spa",
            "categoryName": "Beauty salon",
            "phoneUnformatted": "+17025550166",
            "postalCode": "89109",
        },
        {  # permanently closed
            "title": "Old Town Dental",
            "categoryName": "Dentist",
            "phoneUnformatted": "+17025550171",
            "postalCode": "89102",
            "permanentlyClosed": True,
        },
        {  # no phone
            "title": "Desert Smiles Dental",
            "categoryName": "Dentist",
            "postalCode": "89117",
        },
        {  # no postal code
            "title": "Anytown Dental",
            "categoryName": "Dentist",
            "phoneUnformatted": "+17025550155",
        },
        {  # non-US listing: must be dropped, NOT mangled into a US number
            "title": "London Dental Practice",
            "categoryName": "Dentist",
            "phoneUnformatted": "+442079460958",
            "postalCode": "12345",
            "countryCode": "GB",
        },
    ]
    records = clinic_lookup.places_to_records(items, AppointmentType.DENTAL)
    names = [r["name"] for r in records]
    assert names == ["Fremont Street Dental Care", "Sunridge Family Dentistry", "Copper Ridge Dental"], (
        f"BUG: wrong set of places survived filtering -- got {names}"
    )
    assert records[0] == {
        "name": "Fremont Street Dental Care", "type": "Dentist",
        "phone": "+17025550143", "zipcode": "89101",
    }, f"field mapping / ZIP+4 trim wrong: {records[0]}"
    assert records[1]["phone"] == "+17755550177", "formatted-phone fallback must normalize"
    assert records[2]["type"] == "dentist", (
        "a listing with no category falls back to the searched label"
    )
    assert all(clinic_lookup.validate_record(r) for r in records), (
        "every emitted record must satisfy schemas/clinic_record.schema.json"
    )

    assert clinic_lookup.normalize_phone("+44 20 7946 0958") is None, (
        "BUG: an international number was mangled into a US one -- that dials "
        "a real, unrelated person."
    )
    assert clinic_lookup.normalize_phone("(702) 555-0143") == "+17025550143"
    assert clinic_lookup.extract_zip("V6B 1A1") is None, "no [:5] slicing of non-US codes"
    assert clinic_lookup.zip_distance_miles("89101", "99999") is None, (
        "an out-of-table ZIP must yield None, not a guess"
    )

    # No synthetic fallback: a missing token (same as any other live-search
    # failure) comes back as an empty list, not fabricated clinics -- an
    # earlier version degraded to a committed synthetic dataset here, removed
    # because a fake clinic that LOOKS like a real, callable result is worse
    # than an honest failure (appointment.py raises a clear error on an empty
    # list).
    dump = Path(tempfile.mkdtemp()) / "clinics_last_search.json"
    saved = os.environ.pop("APIFY_API_TOKEN", None)
    try:
        empty = clinic_lookup.find_clinics(USER_ZIP, AppointmentType.DENTAL, dump_path=dump)
    finally:
        if saved is not None:
            os.environ["APIFY_API_TOKEN"] = saved
    assert empty == [], "BUG: a missing token must not quietly hand back synthetic clinics"

    dumped = json.loads(dump.read_text())
    assert dumped["source"] == "apify_google_maps" and dumped["searched_zipcode"] == USER_ZIP
    assert dumped["clinics"] == [], "the dump file must honestly record that nothing was found"
    assert not CALLS, "a lookup places no calls"
    print("\nOK: Maps field mapping, every drop rule (closed / no phone / no ZIP / "
          "non-US / off-category / duplicate), schema validation, the dump file, "
          "and an honest empty result on a missing token -- no synthetic fallback.")


def scenario_roster_load() -> None:
    """The seeded interpreter roster: a TEST FIXTURE only (see
    interpreter_matching.load_candidates_within_radius()'s docstring --
    nothing in a live run reads this file anymore), 29 synthetic records,
    14 in Las Vegas, loaded and bounded by travel distance from the MATCHED
    clinic's ZIP."""
    _reset()
    roster = interpreter_matching._roster()
    assert len(roster) == 29, f"expected 29 interpreters, got {len(roster)}"
    vegas = [r for r in roster if r["city"] == "Las Vegas"]
    assert len(vegas) == 14, f"expected 14 in Las Vegas, got {len(vegas)}"
    assert all(set(r) >= {"name", "age", "phone_number", "zipcode", "expertise"} for r in roster)
    # The invariant that matters is "no roster number can ring a stranger".
    # Two ways to satisfy it: a number in the 555-0100..0199 block reserved for
    # fiction, or one of the operator's own demo test lines (the roster was
    # edited to use those directly, so a live rehearsal rings a phone the
    # operator is holding).
    def _safe_number(number: str) -> bool:
        reserved = number[5:8] == "555" and number[8:].isdigit() and 100 <= int(number[8:]) <= 199
        return reserved or number in calle_run.DEMO_TEST_LINES

    assert all(_safe_number(r["phone_number"]) for r in roster), (
        "BUG: a roster number is neither in the reserved 555-01xx fictional "
        "block nor one of the operator's own test lines -- it could ring a "
        "real, unrelated person."
    )

    near_vegas = interpreter_matching.load_candidates_within_radius("89101", 15.0)
    assert near_vegas, "Las Vegas should have candidates within 15 miles"
    assert all(c.distance_miles is not None and c.distance_miles <= 15.0 for c in near_vegas)
    assert [c.distance_miles for c in near_vegas] == sorted(c.distance_miles for c in near_vegas), (
        "candidates must come back nearest-first"
    )
    assert not any(c.city in ("Reno", "Sparks", "Elko", "Carson City") for c in near_vegas), (
        "BUG: a northern-Nevada interpreter slipped through a 15-mile Vegas radius"
    )
    assert all(c.rate_per_hour is None for c in near_vegas), (
        "rates are asked on the call, never stored in the roster"
    )
    assert near_vegas[0].expertise, "expertise is carried through (stored, not filtered on)"

    fresh = interpreter_matching.load_candidates_within_radius("89101", 15.0)
    near_vegas[0].rate_per_hour = 999.0
    assert fresh[0].rate_per_hour is None and (
        interpreter_matching.load_candidates_within_radius("89101", 15.0)[0].rate_per_hour is None
    ), "BUG: the roster cache handed back a candidate dirtied by a previous run"

    remote = interpreter_matching.load_candidates_within_radius("89801", 1.0)  # Elko, tight radius
    assert len(remote) <= 1, f"a tight remote radius should be nearly empty, got {len(remote)}"
    assert not CALLS, "loading the roster places no calls"
    print("\nOK: 30 synthetic records, 15 in Las Vegas, fictional-block numbers, "
          "radius-bounded nearest-first loading, and no cross-run contamination.")


def scenario_il_interpreter_lookup() -> None:
    """The live Illinois interpreter source's pure parsing and county/region
    tiering, against canned directory rows -- no network. The live fetch
    itself (interpreter_lookup._fetch_directory) is monkeypatched out, same
    spirit as clinic_lookup's canned-items test: the HTTP call is somebody
    else's problem (httpx is pinned and used identically to the Apify path),
    what this build owns is correctly reading what comes back."""
    _reset()

    assert interpreter_lookup.is_illinois_zip("60601"), "Chicago is in the IL centroid table"
    assert not interpreter_lookup.is_illinois_zip(USER_ZIP), "a Nevada ZIP must not read as Illinois"
    assert interpreter_lookup._county_for_zip("60601") == "Cook"
    assert interpreter_lookup._county_for_zip("99999") is None, "an unknown ZIP has no county"

    # Row shape: [name, "City, State", county, region, level, status,
    # disciplined?, email, primary phone, alt phone, deaf interpreter?]
    def _row(name, county, region, status, phone_digits=None):
        phone_cell = f"<a href='tel:+1{phone_digits}'>{phone_digits}</a>" if phone_digits else ""
        return [name, "Somewhere, Illinois", county, region, "General-Advanced", status,
                "No", "<a href='mailto:x@example.com'>x@example.com</a>", phone_cell, "", "No"]

    # IDHHC's own rows pair Cook county with a region ALSO called "Cook" (see
    # the live sample captured during research) -- kept the same way here so
    # a Cook-county clinic's region comes from the rows themselves, not a
    # hardcoded assumption.
    canned_rows = [
        _row("Cook Match", "Cook", "Cook", "Active", "7735550101"),        # tier 1: same county
        _row("Region Match", "DuPage", "Cook", "Active", "6305550102"),    # tier 2: same region, other county
        _row("Wrong Region", "Madison", "West Central", "Active", "6185550103"),  # excluded: different region
        _row("No Phone", "Cook", "Cook", "Active", None),                 # excluded: no phone at all
        _row("Expired License", "Cook", "Cook", "Expired", "7735550104"), # excluded: not Active
    ]

    assert interpreter_lookup._extract_phone("<a href='tel:+17735550101'>7735550101</a>") == "+17735550101"
    assert interpreter_lookup._extract_phone("") is None, "a blank cell has no phone to extract"

    real_fetch = interpreter_lookup._fetch_directory
    interpreter_lookup._fetch_directory = lambda: canned_rows
    try:
        found = interpreter_lookup.find_interpreters_il("60601")  # Chicago, Cook county
    finally:
        interpreter_lookup._fetch_directory = real_fetch

    names = [c.name for c in found]
    assert names == ["Cook Match", "Region Match"], (
        f"BUG: wrong set/order survived county/region tiering and filtering -- got {names}"
    )
    assert found[0].phone == "+17735550101" and found[0].source == "il_directory"
    assert all(c.rate_per_hour is None for c in found), "rates are asked on the call, never assumed"
    assert found[0].expertise == ["General-Advanced license"], (
        "license level is surfaced as expertise, since nothing else is published"
    )

    os.environ["SIGNCALL_DISABLE_IL_LOOKUP"] = "1"

    def _must_not_be_called():
        raise AssertionError("BUG: the live fetch ran despite SIGNCALL_DISABLE_IL_LOOKUP=1")

    interpreter_lookup._fetch_directory = _must_not_be_called
    try:
        assert interpreter_lookup.find_interpreters_il("60601") == [], (
            "SIGNCALL_DISABLE_IL_LOOKUP=1 must short-circuit before any request"
        )
    finally:
        interpreter_lookup._fetch_directory = real_fetch
        os.environ.pop("SIGNCALL_DISABLE_IL_LOOKUP", None)

    assert interpreter_lookup.find_interpreters_il(USER_ZIP) == [], (
        "a ZIP with no Illinois county on file must return empty, not raise or guess"
    )
    assert not CALLS, "parsing and matching interpreters places no calls"
    print("\nOK: Illinois ZIP/county lookup, phone extraction from the directory's own "
          "tel: links, Active+has-a-phone filtering, county-then-region tiering read off "
          "the rows themselves, and SIGNCALL_DISABLE_IL_LOOKUP short-circuiting before any request.")


def scenario_call_routing() -> None:
    """REAL-mode dialling is restricted to the three owned test lines. Tested
    through the pure resolver, because this process is permanently mock
    (MOCK_MODE is bound at import in calle/__init__.py)."""
    _reset()
    lines = calle_run.DEMO_TEST_LINES
    assert len(lines) == 3

    calle_run.reset_call_routing()
    clinic_a, clinic_b, clinic_c = "+17025551001", "+17025551002", "+17025551003"
    assert calle_run.resolve_dial_target(clinic_a, 0) == lines[0]
    assert calle_run.resolve_dial_target(clinic_b, 1) == lines[1]
    assert calle_run.resolve_dial_target(clinic_c, 2) == lines[2]
    # Stickiness: the clinic searched on line 2 is booked on line 2, even
    # though the booking call passes no position at all.
    assert calle_run.resolve_dial_target(clinic_b) == lines[1]
    assert calle_run.resolve_dial_target(clinic_b, 0) == lines[1], (
        "BUG: a later position overrode an existing assignment"
    )
    # Batch positions wrap, and are shared across groups -- freelancer #2 and
    # clinic #2 both land on line 2. They're sequential calls, so only one
    # phone rings at a time.
    assert calle_run.resolve_dial_target("+17025553002", 4) == lines[1]

    for logical in (clinic_a, clinic_b, clinic_c):
        assert calle_run.resolve_dial_target(logical) in lines
        assert calle_run.resolve_dial_target(logical) != logical, (
            "BUG: a logical recipient number was returned as a dial target"
        )

    try:
        calle_run.resolve_dial_target("+17025559998")
    except calle_run.CallRoutingError:
        pass
    else:
        raise AssertionError(
            "BUG: an unassigned recipient with no batch_position silently got a "
            "line instead of raising -- that's how this invariant rots."
        )

    calle_run.reset_call_routing()
    assert calle_run.resolve_dial_target(clinic_b, 2) == lines[2], (
        "BUG: assignments survived a reset and would leak between runs"
    )
    # A family call dials the family member's own number, exactly as entered
    # in the profile -- both the availability ask and the later binding
    # confirm. Clinics and interpreters never dial directly.
    sister = "+17025550123"
    for purpose in (CallPurpose.FAMILY_AVAILABILITY, CallPurpose.FAMILY_CONFIRM):
        assert calle_run.resolve_call_target(sister, purpose, 0) == sister, (
            f"BUG: a {purpose.value} call did not dial the family member directly"
        )
    for purpose in (CallPurpose.CLINIC_SEARCH, CallPurpose.CLINIC_BOOK,
                    CallPurpose.INTERPRETER_AVAILABILITY, CallPurpose.INTERPRETER_CONFIRM):
        assert calle_run.resolve_call_target(sister, purpose, 0) in lines, (
            f"BUG: a {purpose.value} call dialled a number directly"
        )
    # Parallel batches: one number can't take two calls at once, so the same
    # number shares a lock (its calls queue) and different numbers don't.
    assert calle_run._dial_lock(lines[0]) is calle_run._dial_lock(lines[0])
    assert calle_run._dial_lock(lines[0]) is not calle_run._dial_lock(lines[1])
    assert not CALLS
    print(f"\nOK: real-mode dialling resolves only to {', '.join(lines)}, "
          "stickily per recipient, and refuses to guess.")


def scenario_api_endpoint() -> None:
    """The HTTP endpoint the frontend POSTs to, exercised in-process with
    FastAPI's TestClient -- no server, no network, no calls.

    There's no synthetic fallback anymore for either leg (see clinic_lookup.py
    and appointment.py::_resolve_freelance_pool() -- an honest empty result
    beats a fabricated one), so this scenario runs its happy path against an
    ILLINOIS ZIP and mocks the two live TRANSPORTS directly instead:
    clinic_lookup.fetch_places() (so find_clinics() still runs its real
    filtering/sorting code against controlled data, not a live Apify call)
    and interpreter_lookup._fetch_directory() (same reasoning, for
    find_interpreters_il()). That's a closer simulation of a genuinely
    successful live run than the old Nevada-fallback path ever was. The API
    is imported HERE rather than at module scope: importing it sets no
    default mock (registration is per-run), but keeping the import local also
    keeps the other scenarios' deliberately-unmocked tripwires obviously
    untouched.
    """
    _reset()
    os.environ["CALLE_MOCK_MODE"] = "1"
    os.environ["SIGNCALL_API_DEMO_MOCKS"] = "1"
    il_zip = "60616"  # Chicago, Cook county -- see module docstring above

    saved_token = os.environ.get("APIFY_API_TOKEN")
    os.environ["APIFY_API_TOKEN"] = "test-token"  # present, but fetch_places() below never reads it
    real_fetch_places = clinic_lookup.fetch_places
    clinic_lookup.fetch_places = lambda zipcode, appointment_type, token: [{
        "title": "Bridgeport Dental Care", "categoryName": "Dentist",
        "phoneUnformatted": "+13125550199", "postalCode": il_zip,
    }]
    real_fetch_directory = interpreter_lookup._fetch_directory
    interpreter_lookup._fetch_directory = lambda: [[
        "Demo Interpreter", "Chicago, Illinois", "Cook", "Cook",
        "General-Advanced", "Active", "No", "",
        "<a href='tel:+17735550101'>7735550101</a>", "", "No",
    ]]
    try:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from ..api import server  # noqa: PLC0415

        client = TestClient(server.app)

        health = client.get("/health").json()
        assert health["mock_mode"] is True, "the harness must never serve real calls"
        assert health["real_calls_allowed"] is False
        assert health["calle_api_key_present"] in (True, False)
        assert "CALLE_API_KEY" not in str(health), "credentials must never be echoed"

        # 1. A malformed profile is rejected before anything is accepted.
        bad = client.post("/runs", json=_input_json(zipcode="10001"))  # New York -- neither NV nor IL
        assert bad.status_code == 400, f"expected 400, got {bad.status_code}"
        assert "outside this build's coverage" in bad.json()["detail"], (
            "the 400 must carry the validator's own message, not a generic one"
        )
        assert not CALLS, "a rejected profile must not place a call"
        assert not server._runs, "a rejected profile must not create a run"

        from ..api import demo_mocks, notify  # noqa: PLC0415

        events: list[dict] = []
        real_send = notify.send_event
        real_build, real_cancel_build = demo_mocks.build_resolver, demo_mocks.build_cancel_resolver
        real_timeout = server.CONFIRM_TIMEOUT_SECONDS

        def _spying(base_resolver):
            def resolver(task, phone, purpose):
                CALLS.append((phone, task, purpose))
                return base_resolver(task, phone, purpose)

            return resolver

        def spying_build(user, gate=None):
            return _spying(real_build(user, gate))

        def spying_cancel_build():
            return _spying(real_cancel_build())

        notify.send_event = lambda event: events.append(event) or True
        demo_mocks.build_resolver = spying_build
        demo_mocks.build_cancel_resolver = spying_cancel_build

        def start_run() -> dict:
            response = client.post("/runs", json=_input_json(zipcode=il_zip))
            assert response.status_code == 202, (
                f"expected 202, got {response.status_code}: {response.text}"
            )
            return response.json()

        def wait_for(run_id: str, *wanted: str) -> dict:
            status = {}
            for _ in range(200):
                status = client.get(f"/runs/{run_id}").json()
                if status["status"] in wanted:
                    return status
                time.sleep(0.05)
            raise AssertionError(f"run never reached {wanted}; last status {status.get('status')}")

        def confirm(run_id: str, approved) -> "object":
            return client.post(f"/runs/{run_id}/confirm", json={"approved": approved})

        def purposes() -> list:
            return [p for _, _, p in CALLS]

        # 2. A valid profile is accepted, and a second POST is refused while
        #    it's still running. The gate holds the first run open so this is
        #    deterministic rather than a race against a millisecond-fast mock.
        gate = threading.Event()
        server._pending_gate.append(gate)
        body = start_run()
        run_id = body["run_id"]
        assert body["status"] == "running" and body["mock_mode"] is True
        assert "insurance details" in body["plan"], (
            "the plan text must disclose what the booking call hands over"
        )
        assert "approve" in body["plan"], "the plan must say nothing is booked without approval"

        busy = client.post("/runs", json=_input_json())
        assert busy.status_code == 409, f"expected 409 while busy, got {busy.status_code}"
        assert run_id in busy.json()["detail"]

        # 3. Once a clinic and interpreter are lined up the run PAUSES with a
        #    proposal, having placed only non-binding calls, and tells the user.
        gate.set()
        waiting = wait_for(run_id, "awaiting_confirmation")
        proposal = waiting["proposal"]
        assert proposal["interpreter"]["tier"] == "freelance"
        assert proposal["requirements"] == ["Bring a photo ID and your insurance card"]
        assert CallPurpose.CLINIC_BOOK not in purposes(), "nothing may be booked before approval"
        assert CallPurpose.INTERPRETER_CONFIRM not in purposes()
        assert events and events[0]["event"] == "confirmation_requested"
        assert events[0]["run_id"] == run_id and events[0]["proposal"] == proposal
        assert client.post("/runs", json=_input_json()).status_code == 409, (
            "the lock is held while the run waits for the user"
        )

        # 4. Approving books the clinic, then the interpreter confirms.
        assert confirm(run_id, "yes").status_code == 422, "the answer must be a real boolean"
        answered = confirm(run_id, True)
        assert answered.status_code == 200, answered.text
        assert confirm(run_id, True).status_code == 409, "a second answer must not decide twice"
        status = wait_for(run_id, "succeeded", "failed", "declined")
        assert status["status"] == "succeeded", (
            f"run did not succeed: {status.get('error_type')} {status.get('error')}"
        )
        assert status["result"]["appointment"]["booked"] is True
        assert status["result"]["interpreter"]["tier"] == "freelance"
        assert status["result"]["requirements"] == ["Bring a photo ID and your insurance card"]
        assert status["finished_at"] and status["plan"] == body["plan"]
        order = purposes()
        assert order.index(CallPurpose.CLINIC_BOOK) < order.index(CallPurpose.INTERPRETER_CONFIRM)
        assert events[-1]["event"] == "run_finished" and events[-1]["status"] == "succeeded"
        assert events[-1]["result"]["appointment"]["booked"] is True
        assert "_patient_name" not in status, (
            "BUG: get_run() leaked an internal (leading-underscore) field"
        )

        # 4b. The user cancels the succeeded booking: the clinic is called to
        #     cancel, then the confirmed interpreter is released. Only valid
        #     once, and only from succeeded.
        assert status["result"]["clinic_contact"]["phone"], (
            "a succeeded result must carry enough contact info to cancel it later"
        )
        calls_before_cancel = len(CALLS)
        cancel_resp = client.post(f"/runs/{run_id}/cancel")
        assert cancel_resp.status_code == 202, cancel_resp.text
        assert cancel_resp.json()["status"] == "cancelling"
        assert client.post(f"/runs/{run_id}/cancel").status_code == 409, (
            "cancelling while already cancelling must be refused, same as a second confirm answer"
        )
        cancelled = wait_for(run_id, "cancelled", "cancel_failed")
        assert cancelled["status"] == "cancelled", (
            f"cancellation did not succeed: {cancelled}"
        )
        assert cancelled["cancellation"]["clinic_cancelled"] is True
        assert cancelled["cancellation"]["interpreter_released"] is True
        cancel_purposes = [p for _, _, p in CALLS[calls_before_cancel:]]
        assert CallPurpose.CLINIC_CANCEL in cancel_purposes
        assert CallPurpose.INTERPRETER_RELEASE in cancel_purposes
        assert events[-1]["event"] == "run_cancelled" and events[-1]["status"] == "cancelled"
        assert client.post(f"/runs/{run_id}/cancel").status_code == 409, (
            "cancelling an already-cancelled run must be refused too"
        )

        # 4c. Cancelling a run this SERVER PROCESS has forgotten -- the real
        #     scenario a restart between booking and cancelling produces.
        #     This is the one place this scenario reaches into server._runs
        #     directly: there's no HTTP call that simulates "the process
        #     restarted," so the forgetting itself has to be done by hand.
        _reset()
        events.clear()
        run_id = start_run()["run_id"]
        wait_for(run_id, "awaiting_confirmation")
        confirm(run_id, True)
        forgotten = wait_for(run_id, "succeeded", "failed", "declined")
        assert forgotten["status"] == "succeeded", forgotten
        saved_result = forgotten["result"]

        with server._runs_guard:
            del server._runs[run_id]
        assert client.get(f"/runs/{run_id}").status_code == 404, (
            "sanity check: the run must actually be gone from memory now"
        )
        assert client.post(f"/runs/{run_id}/cancel").status_code == 404, (
            "with no body and no memory of the run, there's nothing to adopt it from"
        )

        calls_before_forgotten_cancel = len(CALLS)
        adopted = client.post(
            f"/runs/{run_id}/cancel", json={"result": saved_result, "patient_name": "Dana Reyes"},
        )
        assert adopted.status_code == 202, adopted.text
        resolved = wait_for(run_id, "cancelled", "cancel_failed")
        assert resolved["status"] == "cancelled", (
            f"a run adopted from a supplied body must still cancel cleanly: {resolved}"
        )
        forgotten_purposes = [p for _, _, p in CALLS[calls_before_forgotten_cancel:]]
        assert CallPurpose.CLINIC_CANCEL in forgotten_purposes

        # 5. Saying no ends the run with nothing booked, and frees the lock.
        _reset()
        events.clear()
        run_id = start_run()["run_id"]
        wait_for(run_id, "awaiting_confirmation")
        assert confirm(run_id, False).status_code == 200
        status = wait_for(run_id, "succeeded", "failed", "declined")
        assert status["status"] == "declined", status
        assert status["result"] is None and status["reason"], "a decline is not an error"
        assert not {CallPurpose.CLINIC_BOOK, CallPurpose.INTERPRETER_CONFIRM} & set(purposes())
        assert events[-1]["event"] == "run_finished" and events[-1]["status"] == "declined"

        # 6. No answer counts as no.
        server.CONFIRM_TIMEOUT_SECONDS = 0.3
        _reset()
        run_id = start_run()["run_id"]
        status = wait_for(run_id, "succeeded", "failed", "declined")
        assert status["status"] == "declined" and "No answer" in status["reason"], status
        assert CallPurpose.CLINIC_BOOK not in purposes()
        assert confirm(run_id, True).status_code == 409, "a late answer must be refused"
        server.CONFIRM_TIMEOUT_SECONDS = real_timeout

        assert client.get("/runs/not-a-real-id").status_code == 404
        assert confirm("not-a-real-id", True).status_code == 404
    finally:
        notify.send_event = real_send
        demo_mocks.build_resolver, demo_mocks.build_cancel_resolver = real_build, real_cancel_build
        server.CONFIRM_TIMEOUT_SECONDS = real_timeout
        clinic_lookup.fetch_places = real_fetch_places
        interpreter_lookup._fetch_directory = real_fetch_directory
        if saved_token is not None:
            os.environ["APIFY_API_TOKEN"] = saved_token
        else:
            os.environ.pop("APIFY_API_TOKEN", None)
        os.environ.pop("SIGNCALL_API_DEMO_MOCKS", None)

    print("\nOK: bad input rejected 400 with no run and no calls; a valid one "
          "accepted 202, paused at awaiting_confirmation with a proposal and "
          "only non-binding calls placed, and told the user; approving booked "
          "the clinic then confirmed the interpreter; a second answer was "
          "refused 409; cancelling the succeeded booking cancelled the clinic "
          "and released the interpreter, refused a second cancel, and leaked "
          "no internal fields; a run this process had forgotten (the real "
          "restart-then-cancel scenario) still cancelled cleanly once its own "
          "stored result was supplied; saying no ended it as declined with "
          "nothing booked; no answer timed out to declined; the lock was "
          "released each time.")


SCENARIOS = {
    "clinic_search": scenario_clinic_search,
    "shortcut": scenario_shortcut,
    "decline_then_retry": scenario_decline_then_retry,
    "cheapest_wrong_day": scenario_cheapest_wrong_day,
    "family_locks_in": scenario_family_locks_in,
    "family_confirm_fallback": scenario_family_confirm_fallback,
    "clinic_search_exhausted": scenario_clinic_search_exhausted,
    "requirements": scenario_requirements,
    "clinic_alternate_date_rejected": scenario_clinic_alternate_date_rejected,
    "freelance_parallel": scenario_freelance_parallel,
    "sequential_batch_calls_both": scenario_sequential_batch_calls_both,
    "declined": scenario_declined,
    "interpreters_all_decline": scenario_interpreters_all_decline,
    "input_validation": scenario_input_validation,
    "clinic_lookup": scenario_clinic_lookup,
    "roster_load": scenario_roster_load,
    "il_interpreter_lookup": scenario_il_interpreter_lookup,
    "call_routing": scenario_call_routing,
    "api_endpoint": scenario_api_endpoint,
}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "clinic_search"
    if name not in SCENARIOS:
        print(f"Unknown scenario {name!r}. Options: {list(SCENARIOS)}")
        sys.exit(1)
    SCENARIOS[name]()
