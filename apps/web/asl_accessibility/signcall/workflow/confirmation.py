"""
The user's go/no-go, asked once: after a clinic slot and an interpreter have
both been lined up, and before anything is booked.

The workflow never books on its own authority. `run_interpreter_mesh()` takes
an `approve` callback, shows it a `BookingProposal`, and books only if the
callback returns True. How the question reaches the user (an HTTP round trip,
a text message, a prompt in a terminal) is the caller's business -- see
api/server.py for the HTTP one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class BookingDeclined(Exception):
    """The user said no to the proposed appointment, or never answered.

    An approval callback may raise this itself (for example on a timeout) to
    give a more specific reason than the workflow's default."""


@dataclass
class BookingProposal:
    """What the user is asked to approve. Plain data, so it serializes as-is."""

    clinic_name: str
    clinic_zipcode: str
    clinic_distance_miles: float | None
    date: str  # YYYY-MM-DD
    time: str  # HH:MM, 24-hour
    # Same shape as AppointmentResult.interpreter: {"tier": "user_arranged"},
    # {"tier": "family", "name", "relation"} or {"tier": "freelance", "name",
    # "rate_per_hour", "minimum_hours", "total_estimate", "expertise"}.
    interpreter: dict
    # Freelancers who named this same time and would take over, cheapest
    # first, if the first one declines the final confirmation. Approving the
    # proposal approves them too.
    alternates: list[dict] = field(default_factory=list)
    # What the clinic said is required for the appointment (a referral, ID).
    requirements: list[str] = field(default_factory=list)
    # Other concerns raised on the calls so far, each prefixed with who said it.
    notes: list[str] = field(default_factory=list)


ApprovalFn = Callable[[BookingProposal], bool]
