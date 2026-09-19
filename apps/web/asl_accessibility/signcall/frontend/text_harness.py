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
from ..workflow import clinic_lookup, interpreter_matching  # noqa: E402
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


def _is_booking(purpose: CallPurpose) -> bool:
    return purpose is CallPurpose.CLINIC_BOOK


def _is_confirm(purpose: CallPurpose) -> bool:
    return purpose is CallPurpose.INTERPRETER_CONFIRM


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
) -> ClinicCandidate:
    """Registers this clinic's scripted responses and returns the candidate
    object, so scenarios can assert on what the search wrote back into it.
    `blocked_reason` makes the BOOKING call refuse (booked False); `cancels`
    says whether a later CANCEL call succeeds."""

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


def family(name: str, relation: str, phone: str, *, covers: tuple = (), answers: bool = True) -> dict:
    def resolver(task, phone_, purpose):
        CALLS.append((phone_, task, purpose))
        return _ok({"coverable_slots": list(covers)}) if answers else _no_answer()

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
        age=40, expertise=["medical"], distance_miles=distance,
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
    slot and is locked in, and nobody further is called. The secured family
    member is then TEXTED the confirmation, alongside the user.

    Freelance candidates ARE passed but deliberately left unmocked -- if
    family were skipped or its answer discarded, the run would try to call
    them and raise KeyError loudly instead of quietly succeeding another way.
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
    texts = [e for e in result.evidence if "Confirmation text" in e]
    assert len(texts) == 2 and any("Maya Reyes" in t for t in texts), (
        f"The secured family member is texted the outcome too, got {texts}"
    )
    print("\nOK: family called one at a time in list order after the clinic "
          "match, stopped at the first yes, no freelance call placed, and the "
          "secured family member was texted.")


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


def scenario_freelance_parallel() -> None:
    """A batch of three freelancers is called at the same time, not one after
    another. Each availability call waits at a barrier that only opens once all
    three are in flight, so a sequential search would leave the barrier
    waiting and fail here.

    The cheapest is last in the batch and the rates are out of order, so the
    ranking can't be an accident of which call happened to finish first."""
    _reset()
    thu = _date(3)
    barrier = threading.Barrier(3, timeout=5)
    a = freelancer("A", "+17025553051", covers=(f"{thu} 14:00",), rate=120.0, wait_for=barrier)
    b = freelancer("B", "+17025553052", covers=(f"{thu} 14:00",), rate=100.0, wait_for=barrier)
    c = freelancer("C", "+17025553053", covers=(f"{thu} 14:00",), rate=80.0, wait_for=barrier)
    next_batch = freelancer("D", "+17025553054", covers=(f"{thu} 14:00",), rate=10.0,
                            register=False)
    matched = clinic("Clinic", "+17025551051", 1.0, slots=((thu, "14:00"),),
                     booking_reference="CLINIC-PAR")

    result = run_interpreter_mesh(
        load_user_input(_input_json()), clinics=[matched], candidates=[a, b, c, next_batch]
    )
    print("\n--- RESULT ---")
    print(result)

    assert not barrier.broken, "the three calls never overlapped"
    assert result.interpreter["name"] == "C", "cheapest of the batch wins, whatever finished first"
    assert PROPOSALS[0].interpreter["name"] == "C"
    assert [alt["name"] for alt in PROPOSALS[0].alternates] == ["B", "A"], (
        "fallbacks are ordered cheapest-first regardless of arrival order"
    )
    assert next_batch.phone not in _phones_called(), "a match in the first batch ends the search"
    print("\nOK: all three freelancers were in flight at once, the cheapest won "
          "regardless of arrival order, and no second batch was called.")


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
         _input_json(availability=[{**_slot(3, "2PM"), "day": "sunday"}]),
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
        ("ZIP outside this build's Nevada data",
         _input_json(zipcode="60601"),
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

    dump = Path(tempfile.mkdtemp()) / "clinics_last_search.json"
    saved = os.environ.pop("APIFY_API_TOKEN", None)
    try:
        fallback = clinic_lookup.find_clinics(USER_ZIP, AppointmentType.DENTAL, dump_path=dump)
        assert clinic_lookup.find_clinics(
            USER_ZIP, AppointmentType.DENTAL, allow_fallback=False, dump_path=None
        ) == [], "allow_fallback=False must not quietly hand back synthetic clinics"
    finally:
        if saved is not None:
            os.environ["APIFY_API_TOKEN"] = saved
    assert len(fallback) == 10, f"fallback should fill the 10-clinic cap, got {len(fallback)}"
    assert all(c.source == "fallback_snapshot" for c in fallback), (
        "BUG: fallback data must be tagged so nothing mistakes it for live results"
    )
    assert all(c.clinic_type for c in fallback), "fallback clinics carry a type too"
    distances = [c.distance_miles for c in fallback]
    assert distances == sorted(d for d in distances), "fallback must still be nearest-first"

    dumped = json.loads(dump.read_text())
    assert dumped["source"] == "fallback_snapshot" and dumped["searched_zipcode"] == USER_ZIP
    assert len(dumped["clinics"]) == 10
    assert all(clinic_lookup.validate_record(r) for r in dumped["clinics"]), (
        "the dump file must hold records that satisfy the schema"
    )
    assert not CALLS, "a lookup places no calls"
    print("\nOK: Maps field mapping, every drop rule (closed / no phone / no ZIP / "
          "non-US / off-category / duplicate), schema validation, the dump file, "
          "and a tagged synthetic fallback.")


def scenario_roster_load() -> None:
    """The seeded interpreter roster: 30 synthetic records, half in Las Vegas,
    loaded and bounded by travel distance from the MATCHED clinic's ZIP."""
    _reset()
    roster = interpreter_matching._roster()
    assert len(roster) == 30, f"expected 30 interpreters, got {len(roster)}"
    vegas = [r for r in roster if r["city"] == "Las Vegas"]
    assert len(vegas) == 15, f"expected half in Las Vegas, got {len(vegas)}"
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
    # in the profile. Clinics and interpreters never dial directly.
    sister = "+17025550123"
    assert calle_run.resolve_call_target(sister, CallPurpose.FAMILY_AVAILABILITY, 0) == sister
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

    APIFY_API_TOKEN is popped for the duration so find_clinics() takes the
    committed synthetic fallback instead of firing a live Apify run, and the
    per-run demo mock answers those numbers. The API is imported HERE rather
    than at module scope: importing it sets no default mock (registration is
    per-run), but keeping the import local also keeps the other scenarios'
    deliberately-unmocked tripwires obviously untouched.
    """
    _reset()
    os.environ["CALLE_MOCK_MODE"] = "1"
    os.environ["SIGNCALL_API_DEMO_MOCKS"] = "1"
    saved_token = os.environ.pop("APIFY_API_TOKEN", None)
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
        bad = client.post("/runs", json=_input_json(zipcode="60601"))
        assert bad.status_code == 400, f"expected 400, got {bad.status_code}"
        assert "outside this build's coverage" in bad.json()["detail"], (
            "the 400 must carry the validator's own message, not a generic one"
        )
        assert not CALLS, "a rejected profile must not place a call"
        assert not server._runs, "a rejected profile must not create a run"

        from ..api import demo_mocks, notify  # noqa: PLC0415

        events: list[dict] = []
        real_send, real_build = notify.send_event, demo_mocks.build_resolver
        real_timeout = server.CONFIRM_TIMEOUT_SECONDS

        def spying_build(user, gate=None):
            base = real_build(user, gate)

            def resolver(task, phone, purpose):
                CALLS.append((phone, task, purpose))
                return base(task, phone, purpose)

            return resolver

        notify.send_event = lambda event: events.append(event) or True
        demo_mocks.build_resolver = spying_build

        def start_run() -> dict:
            response = client.post("/runs", json=_input_json())
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
        notify.send_event, demo_mocks.build_resolver = real_send, real_build
        server.CONFIRM_TIMEOUT_SECONDS = real_timeout
        if saved_token is not None:
            os.environ["APIFY_API_TOKEN"] = saved_token
        os.environ.pop("SIGNCALL_API_DEMO_MOCKS", None)

    print("\nOK: bad input rejected 400 with no run and no calls; a valid one "
          "accepted 202, paused at awaiting_confirmation with a proposal and "
          "only non-binding calls placed, and told the user; approving booked "
          "the clinic then confirmed the interpreter; a second answer was "
          "refused 409; saying no ended it as declined with nothing booked; "
          "no answer timed out to declined; the lock was released each time.")


SCENARIOS = {
    "clinic_search": scenario_clinic_search,
    "shortcut": scenario_shortcut,
    "decline_then_retry": scenario_decline_then_retry,
    "cheapest_wrong_day": scenario_cheapest_wrong_day,
    "family_locks_in": scenario_family_locks_in,
    "clinic_search_exhausted": scenario_clinic_search_exhausted,
    "requirements": scenario_requirements,
    "freelance_parallel": scenario_freelance_parallel,
    "declined": scenario_declined,
    "interpreters_all_decline": scenario_interpreters_all_decline,
    "input_validation": scenario_input_validation,
    "clinic_lookup": scenario_clinic_lookup,
    "roster_load": scenario_roster_load,
    "call_routing": scenario_call_routing,
    "api_endpoint": scenario_api_endpoint,
}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "clinic_search"
    if name not in SCENARIOS:
        print(f"Unknown scenario {name!r}. Options: {list(SCENARIOS)}")
        sys.exit(1)
    SCENARIOS[name]()
