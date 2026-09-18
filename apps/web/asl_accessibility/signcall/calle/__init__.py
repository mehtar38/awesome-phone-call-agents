"""
CALL-E adapter. Everything in workflow/ that needs to place a phone call
goes through this package -- nothing else in the app touches the CALL-E
SDK/API directly.

Why the SDK, not MCP, for this project:

The Interpreter Mesh workflow is a deterministic state machine with fully
specified branching (family match? proximity radius? cost ranking? confirm
yes/no?) -- it doesn't need an LLM deciding what to do at each step, only
at the ASL-intent-parsing boundary and when interpreting free-form
transcript nuance. Routing already-decided business logic through an MCP
tool-call loop would add latency, cost, and non-determinism for no benefit.
The SDK (`calle-ai` for Python) is a direct, typed, synchronous-feeling
client that fits explicit control flow. MCP is worth keeping in mind for a
*different* use case -- an interactive dev/ops tool where a human or an
agent wants to place ad hoc test calls conversationally -- but that's not
this workflow. REST is the fallback only if the SDK is ever missing
something (e.g. a specific webhook shape).

Two pieces, matching two different points in the workflow -- corrected
from an earlier draft that assumed a plan/confirm/run split existed at
the SDK level (it doesn't; see plan.py's docstring for why):

  plan.py               -- the accessibility consent gate. Pure local
      logic, zero network calls. Used exactly ONCE per user intent, at
      the top of appointment.py: the plan is rendered back to the Deaf
      user (text + gloss playback) and nothing below reaches CALL-E
      until they approve it.

  call_and_wait() in run.py  -- the ONLY function in this package that
      actually calls CALL-E. Deliberately does NOT use the SDK's own
      create_and_wait()/wait_for_result() -- those only recognize
      {"completed", "failed", "canceled"} as terminal, while CALL-E's own
      documentation lists a wider set (NO_ANSWER, VOICEMAIL, BUSY,
      DECLINED, EXPIRED, ...). Using the SDK's built-in wait would mean a
      call that goes to voicemail silently polls for a full 10-minute
      timeout instead of returning promptly. call_and_wait() does its own
      polling against the fuller, case-insensitive terminal set instead.
      Used for every call the workflow makes after the top-level
      approval above (clinic search, family, interpreter batches, book) --
      these don't re-confirm with the user each time, since they're all part
      of the run already approved once. In REAL mode call_and_wait() also
      routes every one of them onto the three owned demo test lines; see
      DEMO_TEST_LINES / resolve_dial_target() in run.py.

MOCK MODE: set CALLE_MOCK_MODE=1 to make every function in this package
return scripted CallResult objects instead of hitting the real API. This
is what makes it possible to exercise the entire workflow end-to-end --
including the batched clinic search, the family list, and the interpreter
decline/retry logic -- using nothing but a JSON input object, with zero real
calls placed. Useful both for
local development and for the demo dry-run required by the hackathon repo.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Explicit path, not a bare load_dotenv() -- this package is always run
# from apps/ (see the naming-collision note in README.md), so the CWD is
# apps/, not apps/signcall/ where .env actually lives. A bare load_dotenv()
# searches upward from CWD and would miss it. Loaded once here since
# every entry point already imports this package before doing anything
# CALL-E-related.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MOCK_MODE = os.environ.get("CALLE_MOCK_MODE", "0") == "1"
