"""
The one way user input enters this workflow.

`load_user_input(obj)` validates a plain JSON object against
schemas/user_input.schema.json and returns a `UserInput`. The schema file is
the authoritative contract -- validation goes through it directly rather than
through a hand-written mirror of it, so the two can't drift.

What the schema can't express, and this module checks, is everything that
would otherwise fail several real, paid calls deep:

  - the named weekday must actually be that date's weekday
  - no availability date may be in the past (no clinic can book a time that
    has already gone)
  - the ZIP must be inside this build's covered states (the clinic-distance
    centroid table is scoped to Nevada and Illinois; see COVERED_STATES)

`age` and `date_of_birth` are both stored exactly as given and deliberately
NOT cross-checked. Both are read out on the booking call, so an inconsistent
pair is the caller's to fix -- silently "correcting" one would mean telling a
clinic something the user never said.
"""

from __future__ import annotations


import json
from datetime import datetime, timedelta
from pathlib import Path

from .types import (
    AppointmentType,
    FamilyInterpreter,
    Insurance,
    TimeWindow,
    UserInput,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "user_input.schema.json"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
# One file per covered state, same {city, lat, lon} shape (Illinois' entries
# also carry "county", used by workflow/interpreter_lookup.py -- harmless
# extra key for every other consumer, which only reads city/lat/lon).
# Adding a state is adding a file here, nothing else in this list.
COVERED_STATES = {
    "NV": _DATA_DIR / "nv_zip_centroids.json",
    "IL": _DATA_DIR / "il_zip_centroids.json",
}

SLOT_HOURS = 1  # "2PM" means free 2:00PM-3:00PM. Fixed timestamps in, one-hour
                 # windows out -- the user never types a range.

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_schema_cache: dict | None = None
_centroid_cache: dict | None = None
_zip_state_cache: dict | None = None


class UserInputError(ValueError):
    """Raised for anything that makes the input unusable. Always raised
    before a single call is placed."""


def _schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_cache


def zip_centroids() -> dict:
    """ZIP -> {city, lat, lon, ...} merged across every covered state (see
    COVERED_STATES). Shared by clinic_lookup and interpreter_matching so
    there's exactly one distance basis in the app."""
    global _centroid_cache
    if _centroid_cache is None:
        merged: dict = {}
        for path in COVERED_STATES.values():
            merged.update(json.loads(path.read_text(encoding="utf-8"))["zips"])
        _centroid_cache = merged
    return _centroid_cache


def zip_state(zipcode: str) -> str | None:
    """Which covered state (e.g. "NV", "IL") a ZIP belongs to, or None if
    it's in no covered state's centroid file. clinic_lookup.py needs this to
    geocode an Apify search correctly -- a hardcoded state there was a real,
    found bug: every non-NV ZIP resolved to a nonsense location ("60616, NV,
    United States"), which Apify's geocoder can't place, so the live search
    silently returned zero clinics and fell through to the NV-only
    synthetic fallback regardless of which state the user actually typed."""
    global _zip_state_cache
    if _zip_state_cache is None:
        merged: dict = {}
        for state, path in COVERED_STATES.items():
            for zc in json.loads(path.read_text(encoding="utf-8"))["zips"]:
                merged[zc] = state
        _zip_state_cache = merged
    return _zip_state_cache.get(zipcode)


def parse_slot_time(raw: str) -> tuple[int, int]:
    """'2PM' -> (14, 0); '4:30PM' -> (16, 30). The schema already constrains
    the shape; this converts it."""
    text = raw.strip().upper()
    fmt = "%I:%M%p" if ":" in text else "%I%p"
    try:
        parsed = datetime.strptime(text, fmt)
    except ValueError as exc:  # pragma: no cover -- schema should prevent it
        raise UserInputError(f"Unreadable availability time {raw!r}: {exc}") from exc
    return parsed.hour, parsed.minute


def load_user_input(obj: dict, today: "datetime | None" = None) -> UserInput:
    """`today` is injectable so tests can pin 'now' without freezing the clock."""
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover
        raise UserInputError(
            "jsonschema is required to validate user input -- "
            "pip install -r requirements.txt"
        ) from exc

    try:
        jsonschema.validate(obj, _schema())
    except jsonschema.ValidationError as exc:
        location = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise UserInputError(f"User input failed schema validation at {location}: {exc.message}") from exc

    now = today or datetime.now()
    today_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    zipcode = obj["zipcode"]
    if zipcode not in zip_centroids():
        covered = ", ".join(COVERED_STATES)
        raise UserInputError(
            f"ZIP {zipcode} is outside this build's coverage. The clinic "
            f"distance table only covers {covered} -- use a ZIP from one of "
            f"those states, or add a data/<state>_zip_centroids.json and "
            f"register it in user_input.COVERED_STATES first."
        )

    windows: list[TimeWindow] = []
    for entry in obj["availability"]:
        try:
            date = datetime.strptime(entry["date"], "%Y-%m-%d")
        except ValueError as exc:
            raise UserInputError(f"Unreadable availability date {entry['date']!r}: {exc}") from exc
        expected_day = _WEEKDAYS[date.weekday()]
        if entry["day"].lower() != expected_day:
            raise UserInputError(
                f"Availability entry says {entry['day']} but {entry['date']} "
                f"is a {expected_day}. Fix whichever one is wrong -- guessing "
                f"which the user meant would book the wrong day."
            )
        if date < today_date:
            raise UserInputError(
                f"Availability date {entry['date']} is in the past, so no "
                f"clinic could book it."
            )
        hour, minute = parse_slot_time(entry["time"])
        start = date.replace(hour=hour, minute=minute)
        windows.append(TimeWindow(start, start + timedelta(hours=SLOT_HOURS)))

    family = [
        FamilyInterpreter(
            name=member["name"], relation=member["relation"], phone=member["phone_number"]
        )
        for member in obj.get("family_members", [])
    ]

    return UserInput(
        name=obj["name"],
        date_of_birth=obj["date_of_birth"],
        age=obj["age"],
        phone_number=obj["phone_number"],
        zipcode=zipcode,
        appointment_type=AppointmentType(obj["appointment_type"]),
        insurance=Insurance(
            provider_name=obj["insurance"]["provider_name"],
            policy_number=obj["insurance"]["policy_number"],
        ),
        free_windows=windows,
        has_interpreter=obj["has_interpreter"],
        family=family,
    )


def load_user_input_file(path: "str | Path") -> UserInput:
    return load_user_input(json.loads(Path(path).read_text(encoding="utf-8")))
