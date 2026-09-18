"""
Small helpers for reading a CallResult without every caller re-deriving
the same dict access. Polling and result-mapping themselves live in
run.py::call_and_wait() -- there's no separate get_call_run() step in this
design, since call_and_wait() already blocks until a terminal result.
"""

from __future__ import annotations

from .run import CallResult


def was_successful(result: CallResult, min_confidence: float = 0.6) -> bool:
    return result.task_completed and result.completion_confidence >= min_confidence


def evidence_summary(result: CallResult) -> str:
    """A short human-readable digest for the receipt/evidence trail
    (see AppointmentResult.evidence in workflow/types.py)."""
    return " / ".join(result.evidence) if result.evidence else "(no evidence recorded)"
