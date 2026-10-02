"""
Step 3B's live interpreter source for Illinois clinics: the Illinois Deaf and
Hard of Hearing Commission's (IDHHC) own public licensed-interpreter
directory (idhhc.illinois.gov), queried live, per run, never stored.

An earlier version of this module scraped RID's (Registry of Interpreters
for the Deaf) public registry search instead. That was removed: RID's site
explicitly doesn't allow automated querying of it (confirmed on their own
search page, and their robots.txt blocks it), so it couldn't be shipped in a
public, open-source contribution. IDHHC's directory is a different site with
a different posture -- no anti-automation notice anywhere on the page or in
Illinois's site-wide terms, and a robots.txt that names and blocks known
commercial scraper/AI-crawler bots (AhrefsBot, SemrushBot, MJ12bot, CCBot,
...) while leaving a generic client under the default `Allow: /`. It also
has its own "Export To CSV" button on the page -- direct evidence the data
is meant to leave it.

Mechanism: a single unauthenticated GET against the same JSON endpoint the
page's own table widget loads to render itself
(IL_DIRECTORY_URL below) -- not a scrape of rendered HTML, and not the
postback-driven multi-page search RID required. Confirmed by hand: one
request returns the FULL roster in one response (1,087 rows at last check,
no pagination), each row shaped as:

    [name, "City, State", county, region, license level, license status,
     ever disciplined?, email <a>, primary phone <a>, alternate phone <a>,
     licensed deaf interpreter?]

No ZIP or street address is published for an individual interpreter --
only city/county/region -- so matching is by county, not a mile radius.
data/il_zip_centroids.json (the same GeoNames export used for Illinois
clinic-distance centroids) carries each Illinois ZIP's county, which is how
a clinic's ZIP becomes a county to match against this directory's own
COUNTY column.

Filtering: License Status == "Active" (anyone else isn't currently licensed
to practice), and a parseable primary phone number -- a self-reported,
optional field; only 291 of the roster's 1,087 rows have one at last check.
There's no freelance/agency flag in this data (unlike RID's), so every row
that clears those two gates is treated as an independently contactable
interpreter.

Nothing here is cached or written to disk: every call re-fetches live. RID
had an explicit "don't store this" notice; IDHHC doesn't, but the same
no-storage discipline is kept anyway, for the same underlying reason --
these results are a point-in-time license snapshot, not a roster this app
owns. The ONE interpreter who ends up CONFIRMED for a booking is still
recorded normally as part of that booking's own result (see appointment.py)
-- that's an operational engagement record, not a copy of the directory.

SIGNCALL_DISABLE_IL_LOOKUP=1 skips the live request outright (the test
harness sets this automatically; set it by hand for offline dev). A
disabled or failed lookup returns an empty list rather than substituting a
different state's synthetic roster -- an Illinois clinic should fail loudly
("no interpreter found") rather than silently book a fictional Nevada
interpreter.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

from .types import InterpreterCandidate

IL_DIRECTORY_URL = (
    "https://idhhc.illinois.gov/content/soi/idhhc/en/sign-language-interpreters/"
    "directory/jcr:content/responsivegrid/container/container/data_table.datatablejson.json"
)
USER_AGENT = "signcall-interpreter-lookup/1.0 (open-source demo; one lookup per call)"
REQUEST_TIMEOUT_SECONDS = 15.0

IL_ZIP_CENTROIDS_PATH = Path(__file__).resolve().parent.parent / "data" / "il_zip_centroids.json"

_TEL_HREF_RE = re.compile(r"tel:\+?1?(\d{10})")

# Row indices in IL_DIRECTORY_URL's "data" array -- named here so the parser
# below reads like the column it means, not a magic number.
_COL_NAME = 0
_COL_COUNTY = 2
_COL_REGION = 3
_COL_LICENSE_LEVEL = 4
_COL_LICENSE_STATUS = 5
_COL_PRIMARY_PHONE = 8

_il_zip_cache: dict | None = None


def _il_zip_centroids() -> dict:
    """ZIP -> {city, lat, lon, county} for Illinois. Cached for the process
    lifetime, same as every other data file this app loads once."""
    global _il_zip_cache
    if _il_zip_cache is None:
        _il_zip_cache = json.loads(IL_ZIP_CENTROIDS_PATH.read_text(encoding="utf-8"))["zips"]
    return _il_zip_cache


def is_illinois_zip(zipcode: str) -> bool:
    """Whether `zipcode` is one this build has Illinois county data for --
    the dispatch point appointment.py uses to decide live-IL vs. the seeded
    synthetic roster."""
    return zipcode in _il_zip_centroids()


def _county_for_zip(zipcode: str) -> str | None:
    entry = _il_zip_centroids().get(zipcode)
    return entry["county"] if entry else None


def _extract_phone(cell: str) -> str | None:
    """`cell` is either empty or an `<a href='tel:+1XXXXXXXXXX'>...</a>`
    anchor -- pull the number straight from the href rather than the visible
    text, so formatting differences in the text never matter."""
    match = _TEL_HREF_RE.search(cell)
    return f"+1{match.group(1)}" if match else None


def _parse_row(row: list) -> InterpreterCandidate | None:
    """None if this row doesn't clear the Active-license + has-a-phone gate
    (see module docstring) -- never a candidate with no number to call."""
    if row[_COL_LICENSE_STATUS] != "Active":
        return None
    phone = _extract_phone(row[_COL_PRIMARY_PHONE])
    if phone is None:
        return None
    level = row[_COL_LICENSE_LEVEL]
    return InterpreterCandidate(
        name=row[_COL_NAME],
        phone=phone,
        city="",  # not split out of "City, State" -- county is the match key, not city
        age=None,  # not published; never assumed
        expertise=[f"{level} license"] if level else [],
        distance_miles=None,  # no per-interpreter ZIP/address is published -- county-matched, not ranked by distance
        source="il_directory",
    )


def _fetch_directory() -> list[list]:
    response = httpx.get(
        IL_DIRECTORY_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["data"]


def find_interpreters_il(clinic_zip: str) -> list[InterpreterCandidate]:
    """Step 3B's entry point for an Illinois clinic. Live, every call --
    see the module docstring for why nothing is cached.

    Ranks same-county candidates first, then same-region candidates from
    other counties (there's no per-interpreter distance to sort by, so
    region is as close a second tier as this data supports); the batched
    search in interpreter_matching.py then asks them in that order. Nothing
    outside the clinic's own region is ever included -- an unmatched clinic
    fails with "no interpreter found" rather than reaching across the state.
    county->region is read off the directory's own rows (it already pairs
    them on every entry), not hardcoded, so it can't drift from IDHHC's own
    groupings.

    Returns an empty list -- never a different state's roster -- if the
    lookup is disabled, the clinic's county can't be determined, or the live
    request fails."""
    if os.environ.get("SIGNCALL_DISABLE_IL_LOOKUP") == "1":
        return []

    county = _county_for_zip(clinic_zip)
    if county is None:
        return []

    try:
        rows = _fetch_directory()
    except (httpx.HTTPError, ValueError, KeyError):
        return []

    region = next((row[_COL_REGION] for row in rows if row[_COL_COUNTY] == county), None)

    same_county, same_region = [], []
    for row in rows:
        candidate = _parse_row(row)
        if candidate is None:
            continue
        if row[_COL_COUNTY] == county:
            same_county.append(candidate)
        elif region is not None and row[_COL_REGION] == region:
            same_region.append(candidate)
    return same_county + same_region
