"""
Non-CALL-E, lightweight side channels. Deliberately kept separate from
calle/ and from family_call.py -- CALL-E's confirmed API surface is
voice-call-only, so anything here that isn't a phone call just needs *some*
notification mechanism (SMS provider, push notification, email -- pick one
when building the real thing).

The family share-link channel that used to live here is gone: the finalized
workflow makes family call-only, so that path moved to family_call.py as a
real CALL-E call.
"""

from .types import ClinicSlot


def confirmation_text_body(
    recipient_name: str,
    clinic_name: str,
    slot: ClinicSlot,
    interpreter_name: str | None = None,
    requirements: list[str] | None = None,
) -> str:
    """The Step 4 confirmation message. The user and the secured interpreter
    are told the same thing, except that only the user is given
    `requirements` -- what the clinic said is needed for the appointment.

    `interpreter_name` is None when the user brought their own interpreter:
    no contact details are collected for that person, so there's nobody for
    the agent to name and nobody to text -- the user's own message just
    doesn't carry an interpreter line."""
    interpreter_line = (
        f" ASL interpreter: {interpreter_name}." if interpreter_name else ""
    )
    requirements_line = (
        f" Required for the appointment: {'; '.join(requirements)}." if requirements else ""
    )
    return (
        f"{recipient_name}: appointment confirmed at {clinic_name} on "
        f"{slot.date} at {slot.time}.{interpreter_line}{requirements_line}"
    )


def send_confirmation_text(phone: str, message: str) -> None:
    """Step 4's final confirmation text, sent to the user's number and to the
    secured interpreter's (family or freelance, whichever was secured; a
    user-arranged interpreter has no number on file, so that path texts the
    user only).

    A secured FAMILY member is texted here, not called. Family being
    "call-only" governs how they're secured -- the availability ask is always
    a phone call, never a link or a text. Telling them the confirmed outcome
    afterwards is a notification, and goes out the same way the user's does.

    TODO: wire up whatever lightweight notification channel the real build
    uses (SMS provider, email, push) -- this is explicitly NOT a CALL-E
    call, and the finalized design names no provider for it. Callers catch
    this and degrade to an evidence line rather than failing a run whose
    appointment is already genuinely booked.
    """
    raise NotImplementedError(
        "Not a CALL-E call -- plug in an SMS/email/push provider here."
    )


def schedule_follow_up_reminder(appointment: dict, hours_before: int) -> None:
    """A scheduled check closer to the appointment date -- e.g. the
    Accommodation Broker's 24-48h re-verification pattern. Left as a
    stub: needs a real scheduler (cron, a task queue) wired to whatever
    hosts this app."""
    raise NotImplementedError("Wire up a scheduler here.")
