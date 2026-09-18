"""
Pure calendar-intersection logic -- no CALL-E dependency, fully testable
with plain data. Under the 2026-09-13 finalized workflow this is Step 2's
math and nothing else:

    matched = clinic's offered slots  n  user_free

Family no longer appears here at all. The design this replaces matched
family locally on their free windows (C n user_free n family_free); the
finalized one calls each family member and asks which of the ALREADY-matched
slots they can cover, so their availability never has to be modelled as
windows -- see workflow/family_call.py.
"""

from __future__ import annotations


from .types import ClinicSlot, TimeWindow


def slot_in_windows(slot: ClinicSlot, windows: list[TimeWindow]) -> bool:
    """A clinic slot is a single point in time (date + time), not a
    window -- it's "in" a set of free windows if any of them contains it."""
    from datetime import datetime

    slot_dt = datetime.fromisoformat(f"{slot.date}T{slot.time}:00")
    return any(w.start <= slot_dt < w.end for w in windows)


def matched_slots(
    clinic_slots: list[ClinicSlot],
    user_free: list[TimeWindow],
) -> list[ClinicSlot]:
    """Step 2's join: which of this clinic's offered slots can the user
    actually attend. Preserves the clinic's own ordering, so "the first
    matching slot" downstream means first-as-offered, not chronologically
    earliest -- nothing here sorts or normalizes what the clinic said.

    NOTE: no interpreter minimum-hours filtering happens here; that's asked
    per-interpreter later, never assumed upfront (a real bug in an earlier
    draft -- see CALL-E Hackathon Ideas.md)."""
    return [s for s in clinic_slots if slot_in_windows(s, user_free)]
