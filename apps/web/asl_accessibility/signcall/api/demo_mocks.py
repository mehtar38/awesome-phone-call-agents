"""
Scripted answers that let the endpoint be driven end to end without spending
a single real CALL-E call. Enabled only when SIGNCALL_API_DEMO_MOCKS=1.

Why this has to be built per run rather than hardcoded: a mock resolver only
receives `(task, phone)`, but calendar.slot_in_windows() checks a clinic's
offered slot against THIS user's one-hour availability windows with an exact
datetime containment test. A fixed slot would match one profile and fail
every other one the frontend submits. So the resolver closes over the run's
own UserInput and answers with that user's own first free hour -- formatted
"HH:MM", since a clinic answering "2PM" would make datetime.fromisoformat()
raise mid-run.

Scripted outcome: the clinic accepts insurance and offers the user's own
slot, the family list declines, the first freelance interpreter is available
and confirms, and the booking succeeds. That's the full happy path; scenarios
that exercise declines and dead ends live in frontend/text_harness.py.
"""

from __future__ import annotations


import threading

from ..calle.run import CallResult
from ..workflow.types import UserInput

DEMO_RATE_PER_HOUR = 95.0
DEMO_MINIMUM_HOURS = 2.0


def _ok(structured: dict) -> CallResult:
    return CallResult(
        status="completed",
        task_completed=True,
        completion_confidence=0.9,
        structured_result=structured,
    )


def build_resolver(user: UserInput, gate: "threading.Event | None" = None):
    """`gate`, when given, blocks every mocked call until it's set -- the test
    harness uses it to hold a run open long enough to prove the endpoint
    refuses a concurrent second run."""
    window = user.free_windows[0]
    slot_date = window.start.strftime("%Y-%m-%d")
    slot_time = window.start.strftime("%H:%M")
    slot_key = f"{slot_date} {slot_time}"

    def resolver(task: str, phone: str) -> CallResult:
        if gate is not None:
            gate.wait(timeout=30.0)
        if task.startswith("Call this clinic and book"):
            return _ok({
                "booked": True,
                "confirmed_by": "Demo front desk",
                "booking_reference": f"DEMO-{slot_date}",
            })
        if task.startswith("Call this clinic"):
            return _ok({
                "accepts_insurance": True,
                "available_slots": [{"date": slot_date, "time": slot_time}],
            })
        if task.startswith("Call this interpreter back and confirm"):
            return _ok({"confirmed": True})
        if task.startswith("Ask this ASL interpreter"):
            return _ok({
                "coverable_slots": [slot_key],
                "rate_per_hour": DEMO_RATE_PER_HOUR,
                "minimum_hours": DEMO_MINIMUM_HOURS,
            })
        # Family: asked which of the matched slots they can cover. Declining
        # keeps the demo on the freelance path, which is the fuller sequence.
        return _ok({"coverable_slots": []})

    return resolver
