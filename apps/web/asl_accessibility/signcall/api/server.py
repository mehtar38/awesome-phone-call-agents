"""
The endpoint the frontend POSTs to.

    POST /runs               -> 202 {run_id, status, mock_mode, plan}
    GET  /runs/{id}          -> {status, proposal, result | error | reason, plan,
                                 started_at, finished_at}
    POST /runs/{id}/confirm  -> the user's answer: {"approved": true | false}
    POST /runs/{id}/cancel   -> 202, cancels an already-succeeded booking
    GET  /health             -> {mock_mode, real_calls_allowed, credentials present}

A run's status is running -> awaiting_confirmation -> running -> one of
succeeded | declined | failed. A succeeded run can move on once more, only on
the user's own request: succeeded -> cancelling -> cancelled | cancel_failed.

Shape of the contract, and why:

  - The POST body is exactly the object schemas/user_input.schema.json
    defines. It is validated SYNCHRONOUSLY, so a malformed profile comes back
    as 400 with the validator's own message and not one call is placed. That
    fail-fast property is the reason the schema exists.
  - A real run places roughly fifteen phone calls and takes many minutes, so
    an accepted run is handed to a background thread and the caller polls.
    Holding an HTTP connection open across that would time out in any browser.
  - Submission consents to the SEARCH: the plan text returned by POST is
    workflow/appointment.py::describe_goal(), including what each kind of call
    shares. Nothing is BOOKED until a second answer: once a clinic slot and an
    interpreter are lined up, the run pauses in awaiting_confirmation with a
    `proposal`, tells the user (api/notify.py), and waits for POST .../confirm.
    No answer within SIGNCALL_CONFIRM_TIMEOUT_SECONDS (default 900) counts as
    "no", and the run ends as declined.

Run it (from apps/, never from apps/signcall/ -- see README's naming-collision
note):

    CALLE_MOCK_MODE=1 SIGNCALL_API_DEMO_MOCKS=1 \\
        uvicorn signcall.api.server:app --host 127.0.0.1 --port 8000

Do NOT pass --workers >1 or --reload: the one-run-at-a-time lock, the run
registry and calle/run.py's line-assignment map are per-process, so a second
worker would happily start a concurrent run dialling the same three lines.
"""

from __future__ import annotations


import dataclasses
import os
import threading
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool

from ..calle import MOCK_MODE
from ..calle.run import clear_default_mock, register_default_mock
from ..workflow.appointment import cancel_appointment, describe_goal, run_interpreter_mesh
from ..workflow.confirmation import ApprovalFn, BookingDeclined, BookingProposal
from ..workflow.types import UserInput
from ..workflow.user_input import UserInputError, load_user_input
from . import demo_mocks, notify

ALLOW_REAL_CALLS = os.environ.get("SIGNCALL_API_ALLOW_REAL_CALLS") == "1"
USE_DEMO_MOCKS = os.environ.get("SIGNCALL_API_DEMO_MOCKS") == "1"
CONFIRM_TIMEOUT_SECONDS = float(os.environ.get("SIGNCALL_CONFIRM_TIMEOUT_SECONDS", "900"))

# CORS is NOT a security boundary here -- see the README. It stops another
# site's JavaScript reading responses; it stops nothing from POSTing.
LOCALHOST_ORIGIN_REGEX = r"http://(localhost|127\.0\.0\.1)(:\d+)?"

app = FastAPI(
    title="signcall",
    description="Receives a user-input profile and runs the Interpreter Mesh workflow.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=LOCALHOST_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# One run at a time. Beyond politeness: calle/run.py's line-assignment map and
# mock registry are module-level globals, and run_interpreter_mesh() clears the
# former at the start of every run -- so two overlapping runs would erase each
# other's test-line assignments, and the booking call (which relies on a sticky
# assignment made during the clinic search) would fail after the user had
# approved it. The lock is what stands between them, and it is held while a
# run waits for the user's answer.
_run_lock = threading.Lock()
_runs: dict[str, dict] = {}
_runs_guard = threading.Lock()
_active_run_id: str | None = None


@dataclasses.dataclass
class _Decision:
    """The user's answer to a pending confirmation, handed from the request
    thread that received it to the run thread that is waiting on it."""

    answered: threading.Event = dataclasses.field(default_factory=threading.Event)
    approved: bool = False


_decisions: dict[str, _Decision] = {}


class ConfirmRequest(BaseModel):
    approved: StrictBool


def _assert_mode_is_deliberate() -> None:
    """Refuse to serve in real-call mode unless somebody said so twice.

    CALLE_MOCK_MODE defaults to 0 and .env carries a working CALLE_API_KEY, so
    a bare `uvicorn signcall.api.server:app` would otherwise come up ready to
    place real phone calls -- and since submission is the consent, a single
    POST from an open browser tab would spend them with no further click."""
    if MOCK_MODE or ALLOW_REAL_CALLS:
        return
    raise RuntimeError(
        "Refusing to start: this would serve in REAL-CALL mode, where one POST "
        "places around fifteen real phone calls with no further confirmation. "
        "Set CALLE_MOCK_MODE=1 to serve safely, or set "
        "SIGNCALL_API_ALLOW_REAL_CALLS=1 to say you meant it."
    )


_assert_mode_is_deliberate()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _record(run_id: str, **fields) -> None:
    with _runs_guard:
        _runs[run_id].update(fields)


def _finish(run_id: str, **fields) -> None:
    """Records how a run ended, then tells whoever is listening."""
    _record(run_id, finished_at=_now(), **fields)
    notify.send_event({
        "event": "run_finished",
        "run_id": run_id,
        "status": fields["status"],
        "result": fields.get("result"),
        "error": fields.get("error"),
        "reason": fields.get("reason"),
    })


def _approver(run_id: str) -> ApprovalFn:
    """The approval callback for one run: publish the proposal, tell the user,
    and hold the run thread until they answer or the timeout passes."""

    def approve(proposal: BookingProposal) -> bool:
        decision = _Decision()
        proposal_data = dataclasses.asdict(proposal)
        with _runs_guard:
            _decisions[run_id] = decision
            _runs[run_id].update(status="awaiting_confirmation", proposal=proposal_data)
        notify.send_event({
            "event": "confirmation_requested", "run_id": run_id, "proposal": proposal_data,
        })

        decision.answered.wait(timeout=CONFIRM_TIMEOUT_SECONDS)
        with _runs_guard:
            _decisions.pop(run_id, None)
            # An answer can land between the wait timing out and this lock;
            # it was set under the same lock, so it is visible here.
            answered = decision.answered.is_set()
        if not answered:
            raise BookingDeclined(
                f"No answer within {CONFIRM_TIMEOUT_SECONDS:g} seconds, so "
                f"nothing was booked."
            )
        return decision.approved

    return approve


def _execute(run_id: str, user: UserInput, gate: "threading.Event | None") -> None:
    """The background worker. Everything the workflow can raise is recorded as
    a failed run rather than escaping -- the HTTP response was sent long ago,
    so an uncaught exception here would simply vanish."""
    global _active_run_id
    try:
        if USE_DEMO_MOCKS:
            register_default_mock(demo_mocks.build_resolver(user, gate))
        result = run_interpreter_mesh(user, approve=_approver(run_id))
    except BookingDeclined as exc:
        _finish(run_id, status="declined", reason=str(exc))
    except Exception as exc:
        # Some exceptions (a timeout, for one) have an empty message.
        _finish(run_id, status="failed", error=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__)
    else:
        _finish(run_id, status="succeeded", result=dataclasses.asdict(result))
    finally:
        if USE_DEMO_MOCKS:
            clear_default_mock()
        _active_run_id = None
        _run_lock.release()  # in finally, so a raising run can never wedge
                              # the server at 409 forever


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mock_mode": MOCK_MODE,
        "real_calls_allowed": ALLOW_REAL_CALLS and not MOCK_MODE,
        "demo_mocks": USE_DEMO_MOCKS,
        "notify_url_configured": bool(os.environ.get("SIGNCALL_NOTIFY_URL")),        "confirm_timeout_seconds": CONFIRM_TIMEOUT_SECONDS,
        # Presence only -- never the values.
        "calle_api_key_present": bool(os.environ.get("CALLE_API_KEY")),
        "apify_api_token_present": bool(os.environ.get("APIFY_API_TOKEN")),
        "active_run_id": _active_run_id,
    }


@app.post("/runs", status_code=202)
async def start_run(request: Request) -> JSONResponse:
    global _active_run_id

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    # Synchronous on purpose: a malformed profile must be rejected before
    # anything is accepted, let alone dialled.
    try:
        user = load_user_input(payload)
    except UserInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not _run_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                f"A run is already in progress (run_id={_active_run_id}). This "
                f"service handles one run at a time, because concurrent runs "
                f"would place overlapping real phone calls and corrupt each "
                f"other's call routing."
            ),
        )

    run_id = str(uuid.uuid4())
    plan = describe_goal(user)
    with _runs_guard:
        _runs[run_id] = {
            "run_id": run_id, "status": "running", "plan": plan,
            "mock_mode": MOCK_MODE, "started_at": _now(), "finished_at": None,
            "proposal": None, "result": None, "error": None, "error_type": None,
            "reason": None, "cancellation": None,
            # Leading underscore = never returned by get_run() (see there) --
            # this is PII with no reason to leave the process, kept only so a
            # later cancel_appointment() call has the patient's name without
            # needing this UserInput object to still exist.
            "_patient_name": user.name,
        }
    _active_run_id = run_id

    gate = _pending_gate.pop(0) if _pending_gate else None
    threading.Thread(
        target=_execute, args=(run_id, user, gate), name=f"signcall-run-{run_id[:8]}",
        daemon=False,  # a SIGINT mid-run must not kill the process inside the
                       # window where an interpreter has confirmed but the
                       # clinic isn't booked yet
    ).start()

    return JSONResponse(
        status_code=202,
        content={"run_id": run_id, "status": "running", "mock_mode": MOCK_MODE, "plan": plan},
    )


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with _runs_guard:
        run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run with id {run_id}.")
    # Leading-underscore keys are internal bookkeeping (see start_run()) --
    # never returned, on principle: nothing is exposed here that isn't meant
    # to leave the process, and a run_id alone is the only "auth" this
    # endpoint has.
    return {key: value for key, value in run.items() if not key.startswith("_")}


@app.post("/runs/{run_id}/confirm")
def confirm_run(run_id: str, body: ConfirmRequest) -> dict:
    """The user's answer to a run that is awaiting_confirmation. Answering an
    unknown run is 404; answering one that isn't waiting (already answered,
    timed out, or finished) is 409, so a double-click can't decide twice."""
    with _runs_guard:
        run = _runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No run with id {run_id}.")
        decision = _decisions.get(run_id)
        if run["status"] != "awaiting_confirmation" or decision is None or decision.answered.is_set():
            raise HTTPException(
                status_code=409,
                detail=f"Run {run_id} is not waiting for an answer (status: {run['status']}).",
            )
        decision.approved = body.approved
        run["status"] = "running"
        decision.answered.set()
    return {"run_id": run_id, "status": "running", "approved": body.approved}


def _execute_cancel(run_id: str) -> None:
    """The background worker for a cancel request -- mirrors _execute()
    above: cancel_appointment() places real calls (clinic, then whoever was
    interpreting), so this runs off the request thread the same way the
    original booking run did, and the lock it holds is the SAME one, for the
    SAME reason -- a cancellation places calls through the same real-mode
    routing/dial-lock machinery a fresh POST /runs would, and the two must
    never overlap."""
    global _active_run_id
    try:
        if USE_DEMO_MOCKS:
            # A fresh, simpler resolver, not a reuse of the original run's --
            # that one was already cleared in _execute()'s finally block once
            # this run succeeded, and it closed over a UserInput this run may
            # not have anymore anyway (see demo_mocks.build_cancel_resolver()).
            register_default_mock(demo_mocks.build_cancel_resolver())
        with _runs_guard:
            result = _runs[run_id]["result"]
            patient_name = _runs[run_id]["_patient_name"]
        cancellation = cancel_appointment(result, patient_name)
    except Exception as exc:
        _record(
            run_id, status="cancel_failed", finished_at=_now(),
            error=str(exc) or type(exc).__name__, error_type=type(exc).__name__,
        )
        notify.send_event({
            "event": "run_cancelled", "run_id": run_id, "status": "cancel_failed",
            "error": str(exc) or type(exc).__name__,
        })
    else:
        _record(run_id, status="cancelled", finished_at=_now(), cancellation=cancellation)
        notify.send_event({
            "event": "run_cancelled", "run_id": run_id, "status": "cancelled",
            "cancellation": cancellation,
        })
    finally:
        if USE_DEMO_MOCKS:
            clear_default_mock()
        _active_run_id = None
        _run_lock.release()  # in finally, for the same reason as _execute()'s


@app.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str) -> JSONResponse:
    """Cancels an already-succeeded booking: calls the clinic to cancel,
    then releases whoever was lined up to interpret it. Valid from
    `succeeded`, and retriable from `cancel_failed` (nothing about a failed
    cancel attempt un-books the appointment, so trying again is exactly
    what should happen) -- refused from any other status, and refused while
    a cancellation is already in flight, the same way a second confirm
    answer is."""
    global _active_run_id

    with _runs_guard:
        run = _runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No run with id {run_id}.")
        if run["status"] not in ("succeeded", "cancel_failed"):
            raise HTTPException(
                status_code=409,
                detail=f"Run {run_id} has nothing to cancel right now (status: {run['status']}).",
            )

    if not _run_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                f"A run is already in progress (run_id={_active_run_id}). "
                f"Cancellation places real calls through the same routing as "
                f"a booking run, so it has to wait its turn too."
            ),
        )

    with _runs_guard:
        run["status"] = "cancelling"
    _active_run_id = run_id

    threading.Thread(
        target=_execute_cancel, args=(run_id,), name=f"signcall-cancel-{run_id[:8]}",
        daemon=False,
    ).start()

    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "cancelling"})


# Test-only hook: the harness pushes a threading.Event here to hold the next
# run open long enough to prove a concurrent POST is refused. Empty in normal
# operation, so it costs nothing.
_pending_gate: list = []
