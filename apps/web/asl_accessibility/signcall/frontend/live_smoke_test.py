"""
A narrow, standalone diagnostic -- NOT part of the Interpreter Mesh
workflow, not wired into workflow/appointment.py, and not the thing to
extend for a real end-to-end run later. Its only job: place exactly one
real call and print the raw API response next to the mapped CallResult,
so calle/run.py::_map_result()'s field-name assumptions can be checked
against a live response instead of just the documented shape.

This script bypasses calle.run.call_and_wait() (see below), so it enforces
the same routing rule itself: the number argument must be one of
calle.run.DEMO_TEST_LINES. Without that check the app would have exactly one
door through which a real call could reach a number nobody vetted, and the
"real mode only ever dials the three owned test lines" property would be
true of the workflow but not of the app.

Run from apps/ (see README's naming-collision note for why):
    cd apps
    source signcall/.venv/bin/activate
    python3 -m signcall.frontend.live_smoke_test +18722794605     # new call
    python3 -m signcall.frontend.live_smoke_test call_XXXXXXXX     # re-inspect
                                                                     # an existing
                                                                     # call_id instead
                                                                     # of dialing again
                                                                     # (doesn't spend
                                                                     # one of your 20
                                                                     # free calls)

CALLE_API_KEY is read from apps/signcall/.env via python-dotenv (loaded
automatically by calle/__init__.py) -- nothing here needs it typed in.

Deliberately does NOT call calle.run.call_and_wait() as a single opaque
call. Uses its internal helpers directly, in the same sequence
call_and_wait() itself uses, so the intermediate raw dict can be captured
and printed -- calling call_and_wait() and then a second client.calls.get()
afterward would either double the real phone call or waste a network
round-trip re-fetching an already-terminal result.

Raw JSON is printed BEFORE attempting to map it, deliberately -- the first
real run of this script crashed inside _map_result() (completion_confidence
turned out to be a dict, not the plain float the docs implied), and because
the original version mapped-then-printed, the crash hid the exact raw data
that would have made the bug obvious immediately. Printing raw first means
a future mapping bug still leaves you with the diagnostic information you
actually came here for.
"""

import json
import sys

from ..calle.run import DEMO_TEST_LINES, _get_client, _map_result, _poll_until_terminal


def _looks_like_call_id(arg: str) -> bool:
    return arg.startswith("call_")


TASK = (
    "Call this number, say this is a short test call from a hackathon "
    "accessibility project, ask if they can hear clearly, ask them to "
    "say one word back, thank them, and end the call."
)

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "heard_clearly": {"type": "boolean"},
        "note": {"type": "string"},
    },
}


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python3 -m signcall.frontend.live_smoke_test <{' | '.join(DEMO_TEST_LINES)}>")
        print("   or: python3 -m signcall.frontend.live_smoke_test call_XXXXXXXX  (re-inspect, no new call)")
        sys.exit(1)
    arg = sys.argv[1]
    if not _looks_like_call_id(arg) and arg not in DEMO_TEST_LINES:
        print(
            f"Refusing to dial {arg!r}: this build places real calls only to its "
            f"own test lines ({', '.join(DEMO_TEST_LINES)}). Pass one of those, or "
            f"pass an existing call_... id to re-inspect a completed call without "
            f"placing a new one."
        )
        sys.exit(1)
    client = _get_client()

    if _looks_like_call_id(arg):
        call_id = arg
        print(f"Re-inspecting existing call {call_id} -- no new call placed.")
        raw = client.calls.get(call_id)
    else:
        phone = arg
        print(f"Placing one real call to {phone} -- this will actually ring.")
        created = client.calls.create(task=TASK, recipient={"phone": phone}, result_schema=RESULT_SCHEMA)
        call_id = str(created["id"])
        print(f"Call created, id={call_id}. Polling until terminal status...")
        raw = _poll_until_terminal(client, call_id)

    print("\n=== RAW response from client.calls.get() ===")
    print(json.dumps(raw, indent=2, default=str))

    try:
        mapped = _map_result(raw)
    except Exception as exc:
        print(f"\n=== _map_result() CRASHED: {exc!r} ===")
        print("The raw JSON above is still valid -- fix calle/run.py::_map_result() against it.")
        sys.exit(1)

    print("\n=== MAPPED CallResult (what calle/run.py::_map_result() produced) ===")
    print(mapped)

    print("\n=== Check ===")
    missing = []
    if not mapped.status:
        missing.append("status")
    if not mapped.structured_result and raw.get("structured_result"):
        missing.append("structured_result (raw has it, mapped came back empty)")
    if not mapped.transcript_turns and _anywhere_nonempty(raw, "transcript_turns"):
        missing.append(
            "transcript_turns (raw has real turns *somewhere* nested, mapped came back "
            "empty -- this exact bug happened once already: the field lived under "
            "recipients[0].attempts[-1], not at the top level)"
        )
    if missing:
        print(
            f"MISMATCH: {', '.join(missing)} didn't map correctly. "
            f"Compare the raw dict's actual key names above against "
            f"calle/run.py::_map_result() and fix the .get(...) keys there."
        )
    else:
        print("Mapping looks consistent -- status, structured_result, and transcript_turns all came through.")


def _anywhere_nonempty(obj, key: str) -> bool:
    """Recursively search a raw dict/list for a non-empty list under `key`,
    at ANY nesting depth -- deliberately doesn't hardcode today's known
    path, so this keeps working as a check even if the API's nesting
    shape changes again later."""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), list) and obj[key]:
            return True
        return any(_anywhere_nonempty(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_anywhere_nonempty(item, key) for item in obj)
    return False


if __name__ == "__main__":
    main()
