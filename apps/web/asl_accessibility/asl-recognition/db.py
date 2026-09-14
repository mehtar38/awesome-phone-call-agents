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

Only the profile persists here. A submitted booking is forwarded straight to
the external booking workflow (see app.py's /submit-booking) and is never
stored locally -- there is no "past bookings" view in this app, so keeping a
local copy would just be dead data.
"""
import json
import sqlite3
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