"""
Finding clinics to call. No CALL-E dependency -- this is pure lookup.

The user gives a ZIP and an appointment type; this module turns that into up
to 10 nearby clinics, nearest first, for clinic_call.search_clinics() to work
through in batches of 3. Each clinic is exactly four fields -- name, type,
phone, zipcode -- defined by schemas/clinic_record.schema.json and validated
against it before anything downstream sees it.

Mechanism: Apify's Google Maps Scraper (actor compass/crawler-google-places).
It returns the Maps listing itself, so name, category, phone and postal code
arrive as structured fields rather than being scraped out of page markup.
That replaced a Google Custom Search implementation which had to guess all
four out of `pagemap` blobs and snippet text, and had to skip directory
aggregators by hostname to avoid pairing one business's name with another's
phone number. None of that guesswork exists anymore.

What is still true, and worth knowing:

  - Yield is not guaranteed. A place is dropped when it has no callable US
    number, no 5-digit ZIP, is closed, is outside the US, duplicates a phone
    number already seen, or its category doesn't plausibly match what was
    searched for. 20 places are requested to fill a list of 10.
  - Apify meters per place scraped, and a run takes tens of seconds. This is
    a synchronous call -- the workflow waits on it.
  - Distance is computed here from ZIP centroids, not from Maps' own
    ordering, so clinics sharing the user's ZIP all score 0.0 and a ZIP
    outside the Nevada centroid table sorts last.
  - `type` is the listing's own category. Only when Maps returns none does it
    fall back to the label that was searched for, which is the one case where
    it asserts nothing about the clinic.
  - The emergency fallback (data/clinics_fallback.json) is SYNTHETIC, not a
    cached snapshot of real businesses. A committed file of real clinic names
    paired with fetched phone numbers risks publishing a real practice's
    number under the wrong name; the fallback exists to keep a demo alive, so
    it is invented data with reserved 555-01xx numbers, always tagged
    source="fallback_snapshot".
"""

from __future__ import annotations


import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .types import AppointmentType, ClinicCandidate, distance_sort_key
from .user_input import zip_centroids

APIFY_BASE = "https://api.apify.com/v2"
APIFY_ACTOR = "compass~crawler-google-places"  # tilde-encoded "compass/crawler-google-places"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FALLBACK_PATH = DATA_DIR / "clinics_fallback.json"
DEFAULT_DUMP_PATH = DATA_DIR / "clinics_last_search.json"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "clinic_record.schema.json"

MAX_PLACES_REQUESTED = 20   # headroom over the 10-clinic cap -- filtering is lossy
POLL_INTERVAL_SECONDS = 3.0
RUN_DEADLINE_SECONDS = 240.0
HTTP_TIMEOUT_SECONDS = 60.0

# Apify run statuses. Hyphens, not underscores -- and only SUCCEEDED is worth
# fetching a dataset for.
RUN_SUCCEEDED = "SUCCEEDED"
RUN_TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}

# The Maps search phrase for each appointment type.
SEARCH_PHRASES = {
    AppointmentType.PHYSICAL_THERAPY: "physical therapy clinic",
    AppointmentType.EYES: "optometrist",
    AppointmentType.ENT: "ENT doctor",
    AppointmentType.DENTAL: "dentist",
    AppointmentType.MENTAL: "mental health clinic",
    AppointmentType.GENERAL: "primary care clinic",
}

# Keywords that make a Maps category plausibly the thing that was searched
# for. Google Maps returns adjacent and sponsored businesses -- an
# "optometrist" search surfaces sunglasses shops, an "ENT doctor" search
# surfaces med-spas. Nothing downstream reads the clinic's category (the
# CALL-E task text is built from the USER's requested appointment type), so
# without this filter an off-category business would be phoned with a "book a
# dental appointment" script.
CATEGORY_KEYWORDS = {
    AppointmentType.PHYSICAL_THERAPY: ("physical therap", "physiotherap", "rehabilitation", "sports medicine"),
    AppointmentType.EYES: ("optometrist", "ophthalmolog", "eye care", "eye doctor", "optician", "vision"),
    AppointmentType.ENT: ("otolaryngolog", "ear nose", "ent ", "ent doctor", "audiolog", "sinus"),
    AppointmentType.DENTAL: ("dentist", "dental", "orthodont", "endodont", "periodont", "oral surgeon"),
    AppointmentType.MENTAL: ("mental health", "psychiatr", "psycholog", "counselor", "counsell", "therapist", "behavioral health"),
    AppointmentType.GENERAL: ("family practice", "medical clinic", "general practitioner", "primary care",
                               "doctor", "physician", "medical center", "medical group", "walk-in clinic",
                               "urgent care", "internist", "medical office"),
}

_ZIP_RE = re.compile(r"(\d{5})")
# Anchored on purpose: an unanchored search over "+44 20 7946 0958" happily
# returns "+12079460958", a live US number belonging to an unrelated party.
_US_PHONE_RE = re.compile(r"^(?:\+?1)?([2-9][0-9]{2})([2-9][0-9]{2})([0-9]{4})$")

_fallback_cache: dict | None = None
_schema_cache: dict | None = None


# --- record shape --------------------------------------------------------

def _schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_cache


def validate_record(record: dict) -> bool:
    """True when `record` conforms to schemas/clinic_record.schema.json. A
    record that doesn't is dropped by the caller and noted -- one odd postal
    code must never kill an entire lookup."""
    try:
        import jsonschema

        jsonschema.validate(record, _schema())
    except Exception:
        return False
    return True


def record_to_candidate(record: dict, source: str) -> ClinicCandidate:
    return ClinicCandidate(
        name=record["name"],
        phone=record["phone"],
        zipcode=record["zipcode"],
        clinic_type=record["type"],
        source=source,
    )


def candidate_to_record(clinic: ClinicCandidate) -> dict:
    return {
        "name": clinic.name,
        "type": clinic.clinic_type,
        "phone": clinic.phone,
        "zipcode": clinic.zipcode,
    }


# --- extraction ----------------------------------------------------------

def normalize_phone(raw: str) -> str | None:
    """US 10-digit -> E.164, or None. Deliberately anchored to the WHOLE
    string once punctuation is stripped: a substring match would manufacture
    a plausible US number out of an international one and hand it to the
    dialler."""
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"[\s().\-‐-―]", "", raw.strip())
    match = _US_PHONE_RE.match(digits)
    if not match:
        return None
    return "+1" + "".join(match.groups())


def extract_zip(raw: str) -> str | None:
    """First 5-digit run, by match rather than a [:5] slice -- slicing turns
    a Canadian 'V6B 1A1' into 'V6B 1', and any malformed value into a
    plausible-looking wrong ZIP that then misplaces a real clinic on the map."""
    if not isinstance(raw, str):
        return None
    match = _ZIP_RE.match(raw.strip())
    return match.group(1) if match else None


def category_matches(category: str, appointment_type: AppointmentType) -> bool:
    lowered = (category or "").lower()
    return any(keyword in lowered for keyword in CATEGORY_KEYWORDS[appointment_type])


def place_to_record(place: dict, appointment_type: AppointmentType) -> dict | None:
    """One Apify Google Maps dataset item -> a four-field clinic record, or
    None when the place isn't usable. Every field comes from THIS place; none
    is ever borrowed from another."""
    if not isinstance(place, dict):
        return None
    if place.get("permanentlyClosed") or place.get("temporarilyClosed"):
        return None  # can't book at a closed clinic
    country = place.get("countryCode")
    if country and str(country).upper() != "US":
        return None  # this build dials US numbers only

    phone = normalize_phone(place.get("phoneUnformatted") or "") or normalize_phone(place.get("phone") or "")
    if phone is None:
        return None  # can't call a clinic with no number

    zipcode = extract_zip(place.get("postalCode") or "")
    if zipcode is None:
        return None  # can't rank a clinic we can't place

    category = place.get("categoryName")
    if not category:
        categories = place.get("categories")
        category = categories[0] if isinstance(categories, list) and categories else None
    if category and not category_matches(str(category), appointment_type):
        return None  # adjacent/sponsored business, not what was asked for
    # No category at all: fall back to the searched label. This is the one
    # case where `type` asserts nothing about the listing.
    clinic_type = str(category) if category else SEARCH_PHRASES[appointment_type]

    name = (place.get("title") or "").strip()
    if not name:
        return None

    return {"name": name, "type": clinic_type, "phone": phone, "zipcode": zipcode}


def places_to_records(places: list, appointment_type: AppointmentType) -> list[dict]:
    """Maps items -> validated records, de-duplicated by phone. Multi-location
    practices share one central number, and calle.run.resolve_dial_target() is
    sticky per phone -- duplicates in one batch would collapse onto a single
    demo test line and break the per-recipient line assignment."""
    records: list[dict] = []
    seen_phones: set[str] = set()
    for place in places or []:
        record = place_to_record(place, appointment_type)
        if record is None or record["phone"] in seen_phones:
            continue
        if not validate_record(record):
            continue
        seen_phones.add(record["phone"])
        records.append(record)
    return records


# --- distance ------------------------------------------------------------

def haversine_miles(a: "tuple[float, float]", b: "tuple[float, float]") -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.7613 * 2 * math.asin(math.sqrt(h))


def zip_distance_miles(zip_a: str, zip_b: str) -> float | None:
    """None when either ZIP is outside the (Nevada-only) centroid table --
    callers sort those last via types.distance_sort_key rather than guessing."""
    centroids = zip_centroids()
    first, second = centroids.get(zip_a), centroids.get(zip_b)
    if not first or not second:
        return None
    return round(haversine_miles((first["lat"], first["lon"]), (second["lat"], second["lon"])), 1)


# --- Apify transport -----------------------------------------------------

def _actor_input(zipcode: str, appointment_type: AppointmentType) -> dict:
    return {
        "searchStringsArray": [SEARCH_PHRASES[appointment_type]],
        # The actor geocodes locationQuery to a polygon before crawling and
        # errors the run outright when it can't resolve one; a bare 5-digit
        # ZIP is a known weak case, so it's spelled out.
        "locationQuery": f"{zipcode}, NV, United States",
        "maxCrawledPlacesPerSearch": MAX_PLACES_REQUESTED,
        "language": "en",
        "skipClosedPlaces": True,
    }


def fetch_places(zipcode: str, appointment_type: AppointmentType, token: str) -> list:
    """Start the actor, poll it to a terminal status, return its dataset
    items. Raises on any failure; find_clinics() is what decides that a
    failure means "fall back", so nothing here escapes to the workflow."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"}  # not ?token=, so it can't
                                                     # land in a log line or an
                                                     # exception string
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        started = client.post(
            f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs",
            json=_actor_input(zipcode, appointment_type),
        )
        started.raise_for_status()
        run = started.json()["data"]  # run endpoints wrap in {"data": ...}
        run_id, dataset_id = run["id"], run["defaultDatasetId"]

        deadline = time.monotonic() + RUN_DEADLINE_SECONDS
        status = str(run.get("status", ""))
        while status not in RUN_TERMINAL:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Apify run {run_id} still {status!r} after {RUN_DEADLINE_SECONDS}s")
            time.sleep(POLL_INTERVAL_SECONDS)
            polled = client.get(f"{APIFY_BASE}/actor-runs/{run_id}")
            polled.raise_for_status()
            status = str(polled.json()["data"].get("status", ""))

        if status != RUN_SUCCEEDED:
            print("Apify status: {status}")
            raise RuntimeError(f"Apify run {run_id} finished {status}")

        items = client.get(f"{APIFY_BASE}/datasets/{dataset_id}/items", params={"clean": "true"})
        items.raise_for_status()
        return items.json()  


# --- fallback + dump -----------------------------------------------------

def _fallback_records(appointment_type: AppointmentType) -> list[dict]:
    global _fallback_cache
    if _fallback_cache is None:
        _fallback_cache = json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    return [
        {
            "name": entry["name"],
            "type": entry["type"],
            "phone": entry["phone_number"],
            "zipcode": entry["zipcode"],
        }
        for entry in _fallback_cache["clinics"].get(appointment_type.value, [])
    ]


def _dump(path, zipcode: str, appointment_type: AppointmentType, source: str, records: list[dict]) -> None:
    """Inspection side-file. Written atomically so a partial write can't leave
    invalid JSON behind, and wrapped so it can never break a lookup. Holds real
    business names and numbers, which is why it's gitignored."""
    payload = {
        "searched_zipcode": zipcode,
        "appointment_type": appointment_type.value,
        "retrieved": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "clinics": records,
    }
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        temp.replace(path)
    except OSError:
        pass  # inspection only -- never fail a search over it


# --- search --------------------------------------------------------------

def find_clinics(
    zipcode: str,
    appointment_type: AppointmentType,
    limit: int = 10,
    allow_fallback: bool = True,
    dump_path=DEFAULT_DUMP_PATH,
) -> list[ClinicCandidate]:
    """Up to `limit` clinics, nearest first. Returns whatever qualifies --
    fewer than `limit` is an ordinary outcome.

    Every failure mode (missing token, auth error, 402 usage limit, 429, a
    FAILED/ABORTED/TIMED-OUT run, the poll deadline, an unresolvable location,
    or zero places surviving the filters) degrades to the committed synthetic
    fallback, tagged source="fallback_snapshot" so nothing can mistake it for
    live data. Nothing escapes this function: appointment.py calls it
    unguarded, and a demo shouldn't die because an actor run failed.

    `allow_fallback=False` turns that off -- use it when the point is to prove
    the live path actually worked."""
    token = os.environ.get("APIFY_API_TOKEN")
    source = "apify_google_maps"
    records: list[dict] = []
    if token:
        try:
            records = places_to_records(fetch_places(zipcode, appointment_type, token), appointment_type)
        except Exception:
            records = []

    if not records:
        if not allow_fallback:
            return []
        source = "fallback_snapshot"
        records = _fallback_records(appointment_type)

    clinics = [record_to_candidate(record, source) for record in records]
    for clinic in clinics:
        clinic.distance_miles = zip_distance_miles(zipcode, clinic.zipcode)
    clinics.sort(key=lambda c: distance_sort_key(c.distance_miles))
    clinics = clinics[:limit]

    # Dumped AFTER sorting and capping, so the inspection file is exactly what
    # the workflow went on to call -- not a longer list of everything the
    # search happened to turn up.
    if dump_path is not None:
        _dump(dump_path, zipcode, appointment_type, source, [candidate_to_record(c) for c in clinics])
    return clinics
