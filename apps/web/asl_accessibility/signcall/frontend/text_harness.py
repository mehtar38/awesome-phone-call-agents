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
printing a plausible-looking result. Mock resolvers branch on the TASK TEXT as
well as the phone number, because the winning clinic's number is called twice
per run (search, then book) and an interpreter's up to twice (availability,
then binding confirm) -- a per-phone counter alone can't tell those apart.

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

os.environ.setdefault("CALLE_MOCK_MODE", "1")

from ..calle import run as calle_run  # noqa: E402
from ..calle.run import CallResult, register_mock  # noqa: E402
from ..workflow import clinic_lookup, interpreter_matching  # noqa: E402
from ..workflow.appointment import run_interpreter_mesh  # noqa: E402
from ..workflow.types import AppointmentType, ClinicCandidate, InterpreterCandidate  # noqa: E402
from ..workflow.user_input import UserInputError, load_user_input  # noqa: E402

USER_PHONE = "+17025550999"
USER_ZIP = "89101"  # Las Vegas -- this build's data is Nevada-scoped

# Every call placed in a scenario, in order, as (phone, task). Scenarios
# assert against this to prove which calls were NOT placed -- which is where
# most of this design's behaviour lives.
CALLS: list[tuple[str, str]] = []


def _reset() -> None:
    CALLS.clear()


def _is_booking(task: str) -> bool:
    return task.startswith("Call this clinic and book")


def _is_confirm(task: str) -> bool:
    return task.startswith("Call this interpreter back and confirm")


def _phones_called() -> list[str]:
    return [phone for phone, _ in CALLS]


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
) -> ClinicCandidate:
    """Registers this clinic's scripted responses and returns the candidate
    object, so scenarios can assert on what the search wrote back into it."""

    def resolver(task, phone_):
        CALLS.append((phone_, task))
        if _is_booking(task):
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
        })

    register_mock(phone, resolver)
    return ClinicCandidate(
        name=name, phone=phone, zipcode=zipcode, clinic_type="Dentist",
        distance_miles=distance, source="injected",
    )


def family(name: str, relation: str, phone: str, *, covers: tuple = (), answers: bool = True) -> dict:
    def resolver(task, phone_):
        CALLS.append((phone_, task))
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
) -> InterpreterCandidate:
    """`register=False` deliberately leaves this number unmocked, so any call
    to it raises KeyError loudly rather than failing silently."""

    def resolver(task, phone_):
        CALLS.append((phone_, task))
        if not answers:
            return _no_answer()
        if _is_confirm(task):
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
    The finalized workflow end-to-end: a batched, insurance-gated clinic
    search that stops at the first matching batch and tie-breaks by proximity
    within it, then family, then a freelance batch that tie-breaks by rate,
    then the callback booking (the only call that identifies the patient) and
    both confirmation texts.

    Batch 1 fails three different ways -- declines insurance / accepts but no
    overlapping slot / doesn't answer -- so the search moves on. Batch 2 has
    two matches at different distances, proving the nearest-within-batch
    tie-break. The batch-3 clinic must never be called.
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
    never_called = clinic("G (batch 3)", "+17025551007", 6.0, slots=((thu, "14:00"),))

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
        "BUG: the search didn't stop at the first matching batch"
    )
    assert result.appointment["booking_reference"] == "CLINIC-D", (
        "BUG: nearest-within-batch tie-break failed"
    )
    assert result.interpreter["name"] == "J. Kim", (
        f"BUG: cheapest-by-rate should win, got {result.interpreter['name']!r}"
    )
    assert result.family_tier_result == "no_overlap"

    book_idx = next(i for i, (_, t) in enumerate(CALLS) if _is_booking(t))
    confirm_idx = next(i for i, (_, t) in enumerate(CALLS) if _is_confirm(t))
    assert confirm_idx < book_idx, (
        "The finalized sequence confirms the interpreter BEFORE booking"
    )
    booking_task = CALLS[book_idx][1]
    for expected in ("Dana Reyes", "1991-04-12", "Silver State Health", "SSH-4471902"):
        assert expected in booking_task, f"Booking call must give the clinic {expected!r}"
    search_tasks = [t for _, t in CALLS if not _is_booking(t) and not _is_confirm(t)]
    assert all("Dana Reyes" not in t for t in search_tasks), (
        "BUG: a search/availability call leaked the patient's name -- only the "
        "booking call identifies them."
    )
    assert len([e for e in result.evidence if "Confirmation text" in e]) == 2
    print("\nOK: insurance gate, stop-at-first-matching-batch, nearest-within-batch, "
          "cheapest-by-rate, confirm-before-book, patient details on the booking "
          "call only, and both confirmation texts.")


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
    booked_task = next(t for _, t in CALLS if _is_booking(t))
    assert any(f"book the {d} {t} appointment" in booked_task
               for d, t in ((thu, "14:00"), (fri, "10:00"), (mon, "15:00"))), (
        f"Booked slot must be one of the matched slots; task was {booked_task!r}"
    )
    print("\nOK: same clinic search ran, one of the matched slots was booked, "
          "and only the user was notified.")


def scenario_decline_then_retry() -> None:
    """The demo's 'money shot': the cheapest-by-rate interpreter in the
    matching batch declines the binding confirm, and the agent falls through
    to the next match IN THAT SAME BATCH -- without calling any further batch,
    and without a clinic booking existing yet."""
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
    assert sum(1 for _, t in CALLS if _is_booking(t)) == 1
    assert next_batch.phone not in _phones_called(), (
        "BUG: a second batch was called -- the search stops at the first batch "
        "with a match, even if someone in it declines."
    )
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
    booked_task = next(t for _, t in CALLS if _is_booking(t))
    assert f"book the {mon} 15:00 appointment" in booked_task, (
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
    booked_task = next(t for _, t in CALLS if _is_booking(t))
    assert f"book the {mon} 15:00 appointment" in booked_task
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

    assert len(CALLS) == 10, f"Expected exactly 10 clinic calls (3/3/3/1), got {len(CALLS)}"
    assert would_match.phone not in _phones_called(), "BUG: searched past the ceiling"
    print("\nOK: exactly 10 clinics called (batches of 3/3/3/1), ceiling "
          "respected, nothing booked.")


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
    assert good.window_days == 7, f"horizon derives from the furthest slot, got {good.window_days}"
    assert len(good.free_windows) == 3
    first = good.free_windows[0]
    assert (first.start.hour, first.end.hour) == (14, 15), (
        f"'2PM' must mean 14:00-15:00, got {first.start}-{first.end}"
    )
    assert good.free_windows[1].start.hour == 10, "'10AM' must mean 10:00-11:00"
    assert not CALLS, "validation must never place a call"
    print("\nOK: every malformed input was rejected before any call, and a good "
          "one parsed to one-hour windows with a derived 7-day horizon.")


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

        # 2. A valid profile is accepted, and a second POST is refused while
        #    it's still running. The gate holds the first run open so this is
        #    deterministic rather than a race against a millisecond-fast mock.
        gate = threading.Event()
        server._pending_gate.append(gate)
        accepted = client.post("/runs", json=_input_json())
        assert accepted.status_code == 202, f"expected 202, got {accepted.status_code}"
        body = accepted.json()
        run_id = body["run_id"]
        assert body["status"] == "running" and body["mock_mode"] is True
        assert "insurance details" in body["plan"], (
            "the plan text must disclose what the booking call hands over"
        )
        assert "Confirm to proceed?" not in body["plan"], (
            "the run has already started -- don't ask a question nobody answers"
        )

        busy = client.post("/runs", json=_input_json())
        assert busy.status_code == 409, f"expected 409 while busy, got {busy.status_code}"
        assert run_id in busy.json()["detail"]

        gate.set()
        for _ in range(100):
            status = client.get(f"/runs/{run_id}").json()
            if status["status"] != "running":
                break
            time.sleep(0.05)
        assert status["status"] == "succeeded", (
            f"run did not succeed: {status.get('error_type')} {status.get('error')}"
        )
        assert status["result"]["appointment"]["booked"] is True
        assert status["result"]["interpreter"]["tier"] == "freelance"
        assert status["finished_at"] and status["plan"] == body["plan"]

        # 3. The lock is released, so the next run is accepted.
        again = client.post("/runs", json=_input_json())
        assert again.status_code == 202, (
            f"BUG: the lock wasn't released -- got {again.status_code}, so one "
            f"finished run would wedge the server at 409 forever"
        )
        for _ in range(100):
            if client.get(f"/runs/{again.json()['run_id']}").json()["status"] != "running":
                break
            time.sleep(0.05)

        assert client.get("/runs/not-a-real-id").status_code == 404
    finally:
        if saved_token is not None:
            os.environ["APIFY_API_TOKEN"] = saved_token
        os.environ.pop("SIGNCALL_API_DEMO_MOCKS", None)

    print("\nOK: bad input rejected 400 with no run and no calls, a valid one "
          "accepted 202 and polled to a booked appointment, a concurrent POST "
          "refused 409, the lock released afterwards, unknown id 404.")


SCENARIOS = {
    "clinic_search": scenario_clinic_search,
    "shortcut": scenario_shortcut,
    "decline_then_retry": scenario_decline_then_retry,
    "cheapest_wrong_day": scenario_cheapest_wrong_day,
    "family_locks_in": scenario_family_locks_in,
    "clinic_search_exhausted": scenario_clinic_search_exhausted,
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
