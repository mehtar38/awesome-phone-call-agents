"""
Minimal persistence layer -- SQLite, stdlib only, no ORM. This is a deliberate
choice given the timeline: a single-file DB needs no separate server, no
migrations tooling, and ships inside the repo. Swap for Postgres/etc. later
if this ever needs concurrent multi-instance writes; a hackathon demo won't.

There's no login system anywhere in this workflow, so "who is this user"
is a UUID the browser generates once and stores in localStorage (see
static/app.js -- getUserId()), sent back on every request. That's enough to
make a profile persist across visits on the same browser/device without
building real auth under time pressure. It is NOT multi-device identity --
flag that as a known limitation if it matters for the submission.

Two things persist here:

  - the profile (identity, insurance, family), and
  - one `bookings` row per submitted request: where it is in the workflow, the
    proposal the user was asked to approve, and how it ended. The workflow
    reports each of those to /notifications (see app.py), and the browser
    reads them back from here. The row is also the record the future
    cancel/reschedule flow will look appointments up in.
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = "data/app.db"


def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id         TEXT PRIMARY KEY,
            name            TEXT,
            phone           TEXT,
            age             INTEGER,
            dob             TEXT,
            address         TEXT,
            zipcode         TEXT,
            insurance_name  TEXT,
            insurance_id    TEXT,
            family_json     TEXT NOT NULL DEFAULT '[]',
            created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # `phone` and `has_interpreter` were added/removed after this table may
    # already have been created on disk during earlier testing -- CREATE
    # TABLE IF NOT EXISTS won't retrofit an existing file, so patch it here.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
    if "phone" not in existing_cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN phone TEXT")
    # An earlier version kept a `bookings` table with a different shape
    # (id, user_id, payload_json). CREATE TABLE IF NOT EXISTS would leave it in
    # place and every write below would fail, so set it aside rather than
    # delete rows that were saved on purpose.
    booking_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bookings)")}
    if booking_cols and "run_id" not in booking_cols:
        conn.execute("ALTER TABLE bookings RENAME TO bookings_legacy")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            run_id         TEXT PRIMARY KEY,
            user_id        TEXT,
            status         TEXT NOT NULL DEFAULT 'running',
            proposal_json  TEXT,
            result_json    TEXT,
            error          TEXT,
            reason         TEXT,
            created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_profile(user_id: str, profile: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO profiles
            (user_id, name, phone, age, dob, address, zipcode, insurance_name, insurance_id, family_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name, phone=excluded.phone, age=excluded.age, dob=excluded.dob, address=excluded.address,
            zipcode=excluded.zipcode, insurance_name=excluded.insurance_name,
            insurance_id=excluded.insurance_id,
            family_json=excluded.family_json, updated_at=CURRENT_TIMESTAMP
    """, (
        user_id,
        profile.get("name"), profile.get("phone"), profile.get("age"), profile.get("dob"), profile.get("address"),
        profile.get("zipcode"), profile.get("insurance_name"), profile.get("insurance_id"),
        json.dumps(profile.get("family", [])),
    ))
    conn.commit()
    conn.close()


def get_profile(user_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["family"] = json.loads(d.pop("family_json") or "[]")
    return d


# --- bookings ---------------------------------------------------------------
#
# status: running -> awaiting_confirmation -> running -> succeeded | declined | failed
#
# The workflow's notifications can arrive before /submit-booking has recorded
# who the run belongs to (a fast run finishes in milliseconds), so every write
# here is an upsert on run_id that never rolls a run backwards.

@contextmanager
def _transaction():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def register_booking(run_id: str, user_id: str):
    """Links a run to the browser that submitted it. Idempotent."""
    with _transaction() as conn:
        conn.execute("""
            INSERT INTO bookings (run_id, user_id) VALUES (?, ?)
            ON CONFLICT(run_id) DO UPDATE SET user_id=excluded.user_id, updated_at=CURRENT_TIMESTAMP
        """, (run_id, user_id))


def record_proposal(run_id: str, proposal: dict):
    """The workflow is asking the user to approve `proposal`. Ignored if the
    run has already ended, so a late duplicate can't reopen it."""
    with _transaction() as conn:
        conn.execute("""
            INSERT INTO bookings (run_id, status, proposal_json)
            VALUES (?, 'awaiting_confirmation', ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status='awaiting_confirmation', proposal_json=excluded.proposal_json,
                updated_at=CURRENT_TIMESTAMP
            WHERE bookings.status IN ('running', 'awaiting_confirmation')
        """, (run_id, json.dumps(proposal)))


def record_outcome(run_id: str, status: str, result=None, error=None, reason=None):
    """How the run ended: succeeded, declined or failed."""
    with _transaction() as conn:
        conn.execute("""
            INSERT INTO bookings (run_id, status, result_json, error, reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status, result_json=excluded.result_json,
                error=excluded.error, reason=excluded.reason, updated_at=CURRENT_TIMESTAMP
        """, (run_id, status, json.dumps(result) if result is not None else None, error, reason))


def resolve_confirmation(run_id: str, approved: bool):
    """The user answered. Moves an awaiting run on; leaves a run that has
    already ended alone."""
    with _transaction() as conn:
        conn.execute("""
            UPDATE bookings SET status=?, updated_at=CURRENT_TIMESTAMP
            WHERE run_id=? AND status='awaiting_confirmation'
        """, ("running" if approved else "declined", run_id))


def get_booking(run_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    if not row:
        return None
    booking = dict(row)
    proposal, result = booking.pop("proposal_json"), booking.pop("result_json")
    booking["proposal"] = json.loads(proposal) if proposal else None
    booking["result"] = json.loads(result) if result else None
    return booking
