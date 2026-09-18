"""
Placing calls for real. Everything internal to the workflow (Phase 1
discover, Phase 3 fan-out, Phase 4 book/confirm) goes through
`call_and_wait()` here -- it's the only function in this package that
actually reaches CALL-E's API once the top-level consent gate (calle/plan.py)
has been satisfied. No separate `run_call()` step exists anymore -- see
plan.py's docstring for why: there's no SDK/REST "run a previously planned
call" endpoint to call.

Real-mode setup:
    pip install calle-ai              # requires Python >= 3.11
    export CALLE_API_KEY=...          # from your CALL-E account dashboard --
                                       # NOT the same credential as the CLI's
                                       # browser OAuth login (different auth
                                       # domain: api.heycall-e.com vs the
                                       # CLI's seleven-mcp-sg.airudder.com)

Verified against the actual installed SDK (not assumed from docs):
    CalleClient(api_key=...).calls.create(task=..., recipient={"phone": ...},
        result_schema=..., ...) -> dict
    .get(call_id) -> dict

The SDK's own built-in `create_and_wait()`/`wait_for_result()` only treats
{"completed", "failed", "canceled"} as terminal (see calle/calls.py in
site-packages) -- CALL-E's own CLI docs list a wider terminal set
(COMPLETED, FAILED, NO_ANSWER, DECLINED, CANCELED, CANCELLED, VOICEMAIL,
BUSY, EXPIRED, and cased differently). Relying on the SDK's narrower
built-in wait would mean a call that goes to voicemail or isn't answered
silently polls for the full default timeout (10 minutes) before giving up.
`call_and_wait()` below does its own polling against the fuller,
case-insensitive terminal set instead.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from . import MOCK_MODE

# Sourced from CALL-E's CLI documentation (references/commands.md, in the
# `calle` skill) as of 2026-09-12, normalized to uppercase since the
# SDK's own wait_for_result() checks a different, lowercase, narrower set
# (see calle/calls.py in site-packages). Correction from an earlier
# version of this comment: a 2026-09-13 audit checked this against the
# SDK's own *generated* OpenAPI models (calle/generated/models/
# call_status.py) and found they define only
# canceled/completed/failed/in_progress/queued -- none of NO_ANSWER,
# VOICEMAIL, BUSY, DECLINED, or EXPIRED appear anywhere in the installed
# package. So this is NOT a "confirmed inconsistency" the way it was
# previously described -- it's unconfirmed beyond the CLI docs, which
# have since been uninstalled (this app is SDK-only) and can't be
# re-checked from this machine. The set is left as-is since it's a
# harmless superset (the real terminal set is fully covered by
# COMPLETED/FAILED/CANCELED per the generated models) -- but re-verify
# against CALL-E's current developer docs directly if this is ever load-
# bearing, rather than trusting this comment's history.
TERMINAL_STATUSES = {
    "COMPLETED", "FAILED", "NO_ANSWER", "DECLINED",
    "CANCELED", "CANCELLED", "VOICEMAIL", "BUSY", "EXPIRED",
}


@dataclass
class CallResult:
    """Mirrors the shape returned by CALL-E's SDK / get_call_run, mapped
    from the real API's raw dict. Checked against two live calls on
    2026-09-13, not just the documented shape -- that checking found and
    fixed two real bugs (see _extract_confidence and
    _extract_transcript_turns below). Still unverified: the actual TYPES
    of `evidence` (assumed list[str]) and `task_completed` (assumed bool)
    -- both calls returned the expected types, but nothing has forced the
    alternate case yet, so the .get() fallbacks stay defensive rather than
    assumed-safe."""

    status: str
    task_completed: bool
    completion_confidence: float
    structured_result: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    transcript_turns: list[dict] = field(default_factory=list)


_client = None  # lazily constructed real CalleClient, real mode only


def _get_client():
    global _client
    if _client is None:
        import calle as calle_sdk  # the REAL SDK -- resolvable because this
                                     # app is invoked as `signcall.*`, never
                                     # with apps/signcall/ itself on sys.path
                                     # (see README for the naming-collision
                                     # note and the correct run command)
        api_key = os.environ.get("CALLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "CALLE_API_KEY is not set. Get one from your CALL-E account "
                "dashboard (this is separate from the CLI's browser login) "
                "and `export CALLE_API_KEY=...` before running in real mode."
            )
        _client = calle_sdk.CalleClient(api_key=api_key)
    return _client


def _extract_confidence(raw_value) -> float:
    """`completion_confidence` is a dict on the real API (confirmed against
    a live response on 2026-09-13, not the plain float the docs implied --
    this crashed float() outright on the first real call before this fix).
    Handles both shapes defensively since which one is authoritative
    wasn't independently confirmed anywhere else."""
    if isinstance(raw_value, dict):
        score = raw_value.get("score", 0.0)
        return float(score) if score is not None else 0.0
    return float(raw_value or 0.0)


def _extract_transcript_turns(raw: dict) -> list[dict]:
    """Real shape, confirmed against a live response on 2026-09-13: nested
    under recipients[0].attempts[-1], not a top-level key (which doesn't
    exist at all -- the original .get("transcript_turns") silently
    returned [] every time, never actually broken loudly). Single-
    recipient assumption matches how call_and_wait() is always invoked
    today; revisit if/when true batch recipients=[...] calls are added."""
    recipients = raw.get("recipients") or []
    if not recipients or not isinstance(recipients[0], dict):
        return []  # type guard added after edge-case testing confirmed
                    # recipients[0] being a non-dict crashes .get() outright
    attempts = recipients[0].get("attempts") or []
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return []  # same class of guard for attempts
    return attempts[-1].get("transcript_turns") or []


def _map_result(raw: dict) -> CallResult:
    return CallResult(
        status=str(raw.get("status", "")),
        task_completed=bool(raw.get("task_completed", False)),
        completion_confidence=_extract_confidence(raw.get("completion_confidence")),
        structured_result=raw.get("structured_result") or {},
        evidence=raw.get("evidence") or [],
        transcript_turns=_extract_transcript_turns(raw),
    )


def _poll_until_terminal(client, call_id: str, timeout_seconds: float = 600.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        call = client.calls.get(call_id)
        if str(call.get("status", "")).upper() in TERMINAL_STATUSES:
            return call
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for CALL-E call {call_id} to reach a "
                f"terminal status (last seen: {call.get('status')!r})."
            )
        time.sleep(2.0)


# --- Demo call routing (REAL mode only) ---------------------------------
#
# Hard rule for this build: a real call may ring ONLY one of the three test
# lines below, all owned by the project's own user. Clinic numbers are real
# (clinic_lookup fetches them from Google Maps via Apify) and roster numbers are
# synthetic; neither is ever dialed. The logical recipient still drives the
# task text, the mock lookup, and the evidence trail -- only the number the
# SDK is handed is substituted.
#
# Recipients are assigned a line by their POSITION within the group being
# worked through (a batch of 3 clinics or interpreters; the index within the
# family list), and the assignment is STICKY: the clinic searched on line 2
# is booked on line 2, and an interpreter confirmed on line 3 was asked on
# line 3.

DEMO_TEST_LINES = ("+18722794605", "+13126722776", "+19496780146")

_line_assignments: dict[str, str] = {}


class CallRoutingError(RuntimeError):
    """Raised instead of guessing a line. A silent default would be how this
    invariant rots -- the first unthreaded call site would quietly send every
    call to line 1 and nothing would notice."""


def reset_call_routing() -> None:
    """Called at the top of every run. Without this, a second run in the same
    process inherits the first run's line assignments."""
    _line_assignments.clear()


def resolve_dial_target(logical_phone: str, batch_position: int | None = None) -> str:
    """The number actually dialed in real mode. Pure and mode-independent so
    it can be tested directly -- the test harness process is permanently mock
    (MOCK_MODE is bound at import), so this could not otherwise be exercised.

    Never returns `logical_phone`: the result is always a DEMO_TEST_LINES
    entry."""
    assigned = _line_assignments.get(logical_phone)
    if assigned is not None:
        return assigned  # stickiness wins over position
    if batch_position is None:
        raise CallRoutingError(
            f"No test line assigned for {logical_phone!r} and no batch_position "
            f"given. Every call site must pass its index within the group it's "
            f"calling (batch of 3, or family list index) the first time it "
            f"reaches a recipient -- refusing to guess a line."
        )
    line = DEMO_TEST_LINES[batch_position % len(DEMO_TEST_LINES)]
    _line_assignments[logical_phone] = line
    return line


# --- Mock mode plumbing (unchanged) -------------------------------------

MockResolver = Callable[[str, str], CallResult]  # (task, phone) -> CallResult

_mock_resolvers: dict[str, MockResolver] = {}


_default_mock: MockResolver | None = None


def register_default_mock(resolver: MockResolver) -> None:
    """A catch-all used ONLY when no per-number mock matches.

    Exists so the HTTP endpoint can be exercised end to end without spending
    real calls on clinic and interpreter numbers that aren't known until the
    search returns them. It is registered per run and cleared immediately
    afterwards -- never at import -- because several test scenarios
    deliberately leave a number unmocked so that reaching it raises KeyError
    loudly. A default left lying around would turn those tripwires into quiet
    successes.
    """
    global _default_mock
    _default_mock = resolver


def clear_default_mock() -> None:
    global _default_mock
    _default_mock = None


def register_mock(phone: str, resolver: MockResolver) -> None:
    """
    Test harnesses (see frontend/text_harness.py) call this to script what
    a specific phone number "says" when called with a given task. This is
    the whole mechanism that lets Phase 3 fan-out, Phase 4 decline/retry,
    and the rare-reversal path all be exercised with typed text and zero
    real CALL-E credits.
    """
    _mock_resolvers[phone] = resolver


def call_and_wait(
    task: str,
    phone: str,
    result_schema: dict | None = None,
    batch_position: int | None = None,
) -> CallResult:
    """
    The one function everything in workflow/ actually calls.

    `phone` is the LOGICAL recipient -- the clinic or interpreter this call is
    about. In mock mode it selects the scripted response. In real mode it is
    NOT dialed: resolve_dial_target() maps it onto one of the three owned test
    lines, so a real rehearsal can never ring a real clinic. `batch_position`
    is the recipient's index within the group being called and is required the
    first time a given recipient is reached.

    Real mode places an actual phone call and blocks until it reaches a
    terminal status. Only call this after the user has confirmed the plan via
    calle.plan.render_plan_for_user().
    """
    if MOCK_MODE:
        resolver = _mock_resolvers.get(phone) or _default_mock
        if resolver is None:
            raise KeyError(
                f"No mock registered for {phone!r}. Call "
                f"calle.run.register_mock({phone!r}, ...) in your test "
                f"harness before exercising this path."
            )
        return resolver(task, phone)

    client = _get_client()
    dial_target = resolve_dial_target(phone, batch_position)
    created = client.calls.create(
        task=task,
        recipient={"phone": dial_target},
        result_schema=result_schema,
    )
    call_id = str(created["id"])
    final = _poll_until_terminal(client, call_id)
    return _map_result(final)
