"""
The orchestrator. `run_interpreter_mesh(user)` implements the finalized call
sequence from CALL-E Hackathon Ideas.md, Idea 2 (Interpreter Mesh):

    STEP 1  validated user input          (0 calls -- see workflow/user_input.py)
    STEP 2  search & match a clinic       (batches of 3, insurance-gated)
    STEP 3  secure an interpreter         (own | family list | freelance batches)
    STEP 4  book, confirm, notify         (book the matched clinic, text both parties)

Clinics are FOUND, not supplied: clinic_lookup.find_clinics() turns the user's
ZIP and appointment type into up to 10 nearby candidates. Freelance
interpreters come from the seeded state roster within travel distance of the
matched clinic. Both can still be injected for tests.

On commit ordering: this file used to book the clinic BEFORE asking any
interpreter to commit, exploiting the fact that a patient booking is normally
free to cancel while an interpreter engagement isn't. The finalized sequence
deliberately inverts that -- the interpreter is confirmed first, then the
clinic is called back to book for real -- and the residual risk (a real,
fee-bearing engagement with no appointment behind it if that booking call
fails) is an ACCEPTED, explicit trade, not an oversight. See the "Flagged
tension" callout in the design doc, clinic_call._require_booked(), and
README's known limitations. Nothing releases the interpreter automatically
when it happens.

There is no appointment-sensitivity gate anymore. The agent does not classify
a visit or decide whether family is appropriate for it; the user decides, by
setting has_interpreter and by choosing who to register.
"""

from __future__ import annotations


import random

from ..calle.plan import create_plan, render_plan_for_user
from ..calle.run import reset_call_routing

from . import clinic_call, clinic_lookup, family_call, interpreter_matching, reminders
from .types import (
    AppointmentResult,
    ClinicCandidate,
    ClinicSlot,
    InterpreterCandidate,
    UserInput,
)

INTERPRETER_RADIUS_MILES = 15.0


def run_interpreter_mesh(
    user: UserInput,
    clinics: list[ClinicCandidate] | None = None,
    candidates: list[InterpreterCandidate] | None = None,
    confirm_with_user: bool = True,
) -> AppointmentResult:
    """
    `clinics` and `candidates` default to None, which means "go find them"
    (clinic_lookup.find_clinics / interpreter_matching.load_candidates_within_radius).
    Tests pass explicit lists instead so no network or roster lookup is
    involved.
    """

    # Real-mode calls are routed onto the three owned demo lines, and those
    # assignments must not leak between runs in the same process.
    reset_call_routing()

    # --- Top-level consent gate (once per run, not per internal call) -----
    # Purely local -- see calle/plan.py's docstring for why there's no
    # separate "run" step: nothing has touched CALL-E's API yet at this
    # point, and nothing will until the calls below actually fire.
    plan = create_plan(goal=describe_goal(user))
    if confirm_with_user:
        print(render_plan_for_user(plan))  # frontend/ renders this properly;
                                             # a print is enough for the
                                             # text-input test harness

    # --- STEP 2: search & match a clinic ----------------------------------
    if clinics is None:
        clinics = clinic_lookup.find_clinics(user.zipcode, user.appointment_type)
    if not clinics:
        raise RuntimeError(
            f"No {user.appointment_type.value} clinic could be found near "
            f"{user.zipcode} -- nothing to call."
        )
    clinic, matched = clinic_call.search_clinics(
        clinics, user.free_windows, user.window_days
    )
    if clinic is None:
        raise RuntimeError(
            "No clinic in the search list both accepts insurance and has a "
            "slot the user can attend -- nothing to book."
        )

    # --- STEP 3: secure an interpreter ------------------------------------
    if user.has_interpreter:
        return _run_shortcut(user, clinic, matched)

    if user.family:
        member, slot = family_call.call_family_in_order(user.family, matched)
        if member is not None:
            return _book_and_notify(
                user,
                clinic,
                slot,
                interpreter_name=member.name,
                interpreter_phone=member.phone,
                interpreter_detail={
                    "tier": "family",
                    "name": member.name,
                    "relation": member.relation,
                },
                family_tier_result="locked_in",
                interpreter_source="family",
                extra_evidence=[
                    f"Family member {member.name} ({member.relation}) locked in "
                    f"for {slot.date} {slot.time} (list order, stopped there)."
                ],
            )

    return _book_with_freelancer(user, clinic, matched, candidates)


def describe_goal(user: UserInput) -> str:
    """What the user actually approves. States the search breadth AND the
    data sharing, because this run can phone up to 10 clinics plus a family
    list plus batches of interpreters, and the booking call hands over the
    user's identifying and insurance details. The consent gate is the one
    place that has to say so."""
    return (
        f"Book a {user.appointment_type.value.replace('_', ' ')} appointment "
        f"near {user.zipcode} within {user.window_days} days and secure an ASL "
        f"interpreter for it. To do that I'll phone nearby clinics (up to 10, "
        f"nearest first, 3 at a time, stopping at the first that accepts "
        f"insurance and has a time you're free), then "
        + ("your family list one at a time, then freelance interpreters 3 at a "
           "time, " if not user.has_interpreter else "")
        + f"and finally call that clinic back to book. On the booking call "
        f"only, I'll give the clinic your name, date of birth, age, phone "
        f"number and insurance details ({user.insurance.provider_name}, policy "
        f"{user.insurance.policy_number}) so they can put the appointment in "
        f"their book. The earlier calls never mention you."
    )


def _run_shortcut(
    user: UserInput, clinic: ClinicCandidate, matched: list[ClinicSlot]
) -> AppointmentResult:
    """SHORTCUT: the user already has their own interpreter, so Step 3 is
    trivially satisfied by Step 2's clinic match and only Step 4 remains.

    Two deliberate consequences:
      - No contact details are collected for that interpreter, so the agent
        can't call or text them. Step 4 notifies the user only, and the
        result carries no interpreter name or number.
      - Their availability isn't collected either, so clinic slots are
        matched against the user's own windows exactly as on the full path,
        and where several match, one is picked at random.
    """
    slot = random.choice(matched)
    return _book_and_notify(
        user,
        clinic,
        slot,
        interpreter_name=None,
        interpreter_phone=None,
        interpreter_detail={"tier": "user_arranged"},
        family_tier_result="not_applicable",
        interpreter_source="user_arranged",
        extra_evidence=[
            f"User arranged their own interpreter; no contact details on file, "
            f"so none was called or texted. Slot chosen at random from "
            f"{len(matched)} matched slot(s)."
        ],
    )


def _book_with_freelancer(
    user: UserInput,
    clinic: ClinicCandidate,
    matched: list[ClinicSlot],
    candidates: list[InterpreterCandidate] | None,
) -> AppointmentResult:
    """Step 3B then Step 4. The batch search stops at the first batch with a
    match; on a decline at the binding confirm we fall through to the next
    match WITHIN that batch, since the design explicitly stops calling further
    interpreters once a batch has matched."""
    if candidates is None:
        candidates = interpreter_matching.load_candidates_within_radius(
            clinic.zipcode, INTERPRETER_RADIUS_MILES
        )
    if not candidates:
        raise RuntimeError(
            f"No interpreter in the roster is within {INTERPRETER_RADIUS_MILES} "
            f"miles of {clinic.name} ({clinic.zipcode}) -- nobody was called, "
            f"and no clinic appointment was taken."
        )

    ranked = interpreter_matching.search_freelancers_in_batches(candidates, matched)
    if not ranked:
        raise RuntimeError(
            "No freelance interpreter in any batch can cover a matched slot "
            "-- nothing booked, and no clinic appointment was taken."
        )

    for candidate in ranked:
        slot = interpreter_matching.slot_for_candidate(candidate, matched)
        if slot is None:
            continue  # can't happen for a batch match, but never guess a slot
        if interpreter_matching.confirm_interpreter(candidate, slot):
            return _book_and_notify(
                user,
                clinic,
                slot,
                interpreter_name=candidate.name,
                interpreter_phone=candidate.phone,
                interpreter_detail={
                    "tier": "freelance",
                    "name": candidate.name,
                    "rate_per_hour": candidate.rate_per_hour,
                    "minimum_hours": candidate.minimum_hours,
                    "total_estimate": candidate.total_cost(),
                    "expertise": candidate.expertise,
                    "confirmed": True,
                },
                family_tier_result="no_overlap" if user.family else "not_registered",
                interpreter_source="freelance",
                extra_evidence=[
                    f"{candidate.name} confirmed for {slot.date} {slot.time} "
                    f"at ${candidate.rate_per_hour}/hr (cheapest by rate in "
                    f"their batch)."
                ],
            )
        # NO / no answer: nothing has been booked yet, so a decline costs
        # nothing but the call -- try the next match in the same batch.

    raise RuntimeError(
        "Every matching interpreter in the first matching batch declined; "
        "no clinic appointment was taken, so nothing needs cancelling."
    )


def _book_and_notify(
    user: UserInput,
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    interpreter_name: str | None,
    interpreter_phone: str | None,
    interpreter_detail: dict,
    family_tier_result: str,
    interpreter_source: str,
    extra_evidence: list[str],
) -> AppointmentResult:
    """STEP 4, shared by every path: call the matched clinic back to book the
    slot for real -- giving them the patient's details, which no earlier call
    did -- then text the user and whichever interpreter was secured.

    The booking call raises if it didn't actually book (see
    clinic_call._require_booked); when the agent secured an interpreter, that
    interpreter has already committed, which is the accepted residual risk of
    the finalized ordering.

    NOT implemented: the design's "add the appointment to the user's
    calendar" step. No mechanism is specified for it and none is invented
    here; see README.
    """
    booking = clinic_call.book_slot(clinic, slot, user, interpreter_name)
    distance = (
        f"{clinic.distance_miles} mi" if clinic.distance_miles is not None else "distance unknown"
    )
    evidence = [
        f"Clinic {clinic.name} [{clinic.clinic_type}] ({clinic.zipcode}, "
        f"{distance}, source={clinic.source}) matched and booked for "
        f"{slot.date} {slot.time}.",
        *extra_evidence,
    ]
    evidence.extend(
        _send_confirmations(user, clinic, slot, interpreter_name, interpreter_phone)
    )
    return AppointmentResult(
        appointment=booking,
        interpreter=interpreter_detail,
        family_tier_result=family_tier_result,
        interpreter_source=interpreter_source,
        cancellation_deadline=None,  # TODO: derive from the interpreter's own
                                      # policy, once real interpreter data exists
        evidence=evidence,
    )


def _send_confirmations(
    user: UserInput,
    clinic: ClinicCandidate,
    slot: ClinicSlot,
    interpreter_name: str | None,
    interpreter_phone: str | None,
) -> list[str]:
    """Both confirmation texts -- or just the user's, when the user brought
    their own interpreter and the agent has no way to reach them. No SMS
    provider is wired up (the design names none), so each send degrades to an
    evidence line instead of failing a run whose appointment is genuinely
    booked."""
    recipients = [("user", user.phone_number, "You")]
    if interpreter_phone and interpreter_name:
        recipients.append(("interpreter", interpreter_phone, interpreter_name))

    lines = []
    for label, phone, recipient_name in recipients:
        body = reminders.confirmation_text_body(
            recipient_name, clinic.name, slot, interpreter_name
        )
        try:
            reminders.send_confirmation_text(phone, body)
        except NotImplementedError:
            lines.append(
                f"Confirmation text to {label} ({phone}) NOT sent: no "
                f"SMS provider wired up. Message was: {body}"
            )
        else:
            lines.append(f"Confirmation text sent to {label} ({phone}).")
    if interpreter_phone is None:
        lines.append(
            "No interpreter notification: the user arranged their own "
            "interpreter and no contact details were collected for them."
        )
    return lines
