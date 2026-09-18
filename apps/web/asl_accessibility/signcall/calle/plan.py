"""
The top-level consent gate: build the plan text, render it to the user,
wait for their explicit confirmation -- BEFORE anything ever touches
CALL-E's API.

Correction from an earlier draft of this file: this used to assume CALL-E
exposed a two-step "plan_call (draft only) -> confirm -> run_call (dial)"
flow at the SDK/REST level, mirroring the MCP tool names. Direct inspection
of the installed `calle-ai` SDK (calle/calls.py in site-packages) shows
that's not the case -- `client.calls.create()` immediately creates AND
dispatches the call; there is no "plan without dialing" endpoint at the
SDK/REST layer. That two-step shape is specifically an MCP-tool
abstraction (`plan_call` / `run_call` as separate MCP tools), not
something the SDK or REST API provide.

The fix: the consent gate lives entirely in THIS app's own code, not in
CALL-E's. `create_plan()` below makes zero network calls -- it's pure
local logic that builds the goal text and hands it back for the user to
approve. Nothing in this module ever reaches CALL-E. The actual API call
only happens later, in calle/run.py::call_and_wait(), and only after
appointment.py has already gotten the user's confirmation. This is
arguably a stronger accessibility property than what the MCP two-step
gate would have given us: literally nothing touches CALL-E before the
Deaf user has explicitly approved the plan, not even a draft.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlanHandle:
    goal_text: str  # what gets rendered back to the user (text + gloss)


def create_plan(goal: str) -> PlanHandle:
    """
    Purely local. `goal` should be the fully-specified natural-language
    description of what the run will do -- see
    workflow/appointment.py::describe_goal(), which states both the search
    breadth (how many clinics get phoned) and the data sharing (the booking
    call gives the clinic the patient's name, date of birth, age, phone and
    insurance details). Both matter here: this text is the entire basis on
    which the user consents.
    """
    return PlanHandle(goal_text=goal)


def render_plan_for_user(plan: PlanHandle) -> str:
    """
    What gets shown back to the Deaf user (as text; the frontend layer is
    responsible for the accompanying gloss playback) before they confirm.
    Nothing in calle/run.py fires until the caller has gotten a yes here.
    """
    return f"Here's what I'll do:\n\n{plan.goal_text}\n\nConfirm to proceed?"
