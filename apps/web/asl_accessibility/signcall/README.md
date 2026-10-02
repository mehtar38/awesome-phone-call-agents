# signcall

An ASL accessibility layer for CALL-E: sign or type your intent, the agent phones the hearing world to book the appointment. Built for the *CALL-E: Your Code Is Calling* hackathon. Full problem statement, evidence base, and the finalized workflow spec live in `../../CALL-E Hackathon Ideas.md` (Idea 2, Interpreter Mesh) — this repo is the implementation of that spec.

This directory lives at `apps/web/asl_accessibility/signcall/`. Its sibling, `apps/web/asl_accessibility/asl-recognition/`, is the actual frontend — a camera-capture + typed-text web app that turns a user's intent into the JSON this app's `POST /runs` expects (see "Run the API" below). Every command in this README is written to run from `apps/web/asl_accessibility/`, the parent of both.

## The core architecture decision

**CALL-E has no visual input surface at all.** Its entire interface — the MCP tools, the SDK's `client.calls.create()`, `POST /v1/calls` — takes natural-language **text**. There's no way to hand it a video frame or a keypoint sequence. So the pipeline has exactly one shape:

```
webcam clip → [ASL recognizer] → gloss sequence → [LLM] ─┐
                                                          ├→ user input JSON (schemas/user_input.schema.json)
                     registered profile: identity, ZIP, ─┘
                     insurance, family list                         │
                                                                    ▼
                                              workflow/ + calle/  (pure text/JSON from here on)
                                                                    │
                                                                    ▼
                                                    CALL-E places real phone calls
                                                                    │
                                                                    ▼
                                     structured_result → rendered back as text + gloss playback
```

This isn't a design choice we made for convenience — it's forced by CALL-E's own interface. The useful consequence: **everything past the input JSON is testable today, with zero ASL model and zero real CALL-E credits.** The contract is `schemas/user_input.schema.json`, parsed by `workflow/user_input.py` — a plain JSON object in, a validated `UserInput` out.

Worth being precise about the seam, because it's easy to overstate: a sign-language recognizer emits *gloss*, and gloss cannot plausibly carry a policy number, a date of birth, or a family member's phone number. So the JSON is what the **agent** consumes, and it's assembled from two sources — the part a user expresses per visit (appointment type, availability, whether they already have an interpreter) and the part that lives in a registered profile (identity, ZIP, insurance, the family list). `../asl-recognition/` is what actually assembles that object today (profile capture, camera/typed intent capture, and the HTTP round-trip to this app); `frontend/text_harness.py` in this directory builds the same shape of object directly, for testing this app in isolation.

## CALL-E integration: SDK only

This app uses the **`calle-ai` Python SDK exclusively**. Nothing in this codebase invokes the `calle` CLI, MCP tools, or raw REST — one integration path, no mixing. (An earlier pass through this project briefly installed the CLI and its agent skill while exploring the CALL-E ecosystem; both have since been fully uninstalled — global npm package removed, skill removed, OAuth session logged out, local cache cleared. They played no role in this app's code and are gone.)

**Why the SDK and not the alternatives**, briefly:
- MCP's `plan_call`/`run_call`/`get_call_run` tools exist for an LLM deciding what to do at each conversational turn. This workflow is a fully-specified deterministic state machine instead (family match? proximity? cost rank? yes/no on a confirm call?) — none of that needs an LLM in the loop, so MCP would add latency and non-determinism for nothing.
- The CLI + its agent skill are built for a *chat-based AI agent* placing calls on a human's behalf mid-conversation, which is a different threat model (verifying binary identity before every shell-out, treating all output as untrusted) than *this app's own trusted internal code* calling a pinned dependency.
- The SDK is what a backend service should call directly — a typed, importable client, not a subprocess wrapper — and reads as more idiomatic "skillful CALL-E usage" (a named judging criterion) for other contributors extending this app.

**One credential, one place it's used:** a static API key (`Authorization: Bearer`) from your CALL-E account dashboard, set as `CALLE_API_KEY` and read only by `calle/run.py`. Nothing else in this repo needs, stores, or reads any CALL-E credential.

### The consent gate is two-stage, and it's pure application logic, not a CALL-E feature

**Submitting a request consents to the SEARCH, not to a booking.** `POST /runs` validates the input, hands back `plan` — the exact text of `workflow/appointment.py::describe_goal()`, which states in plain language which clinics will be phoned, what's said on those calls, and that nothing is booked without a second, separate approval — and then starts placing calls in the background: clinic search calls and interpreter availability calls both happen at this stage, because finding out who's available **is** the search the user just asked for. Nothing is **booked**, and no interpreter is **bindingly confirmed**, until the run reaches `awaiting_confirmation` with a concrete `proposal` (a specific clinic, date, time, and interpreter) and the user answers `POST /runs/{id}/confirm` with `true`. A `false` answer, or no answer within `SIGNCALL_CONFIRM_TIMEOUT_SECONDS` (default 900s), ends the run as `declined` with nothing booked and nothing charged.

An earlier draft of this project assumed CALL-E exposed a two-step "plan without dialing, then confirm, then run" flow at the SDK/REST level, mirroring the MCP tool names, and built a `calle/plan.py` to pre-render that plan. **Direct inspection of the installed SDK's source (`calle/calls.py`) showed that's wrong** — `client.calls.create()` immediately creates *and dispatches* the call; there's no "plan-only" endpoint outside MCP, so there was never anything for a separate `calle/plan.py` module to gate. The actual consent property lives entirely in `workflow/appointment.py` and `api/server.py` instead (described above), and that file doesn't exist in this codebase anymore.

### A correctness gap found by reading the SDK's actual code, not its docs

The SDK's own `wait_for_result()` only treats `{"completed", "failed", "canceled"}` as terminal (lowercase). CALL-E's own CLI documentation lists a much wider terminal set — `COMPLETED`, `FAILED`, `NO_ANSWER`, `DECLINED`, `CANCELED`, `CANCELLED`, `VOICEMAIL`, `BUSY`, `EXPIRED` (uppercase). These two pieces of official CALL-E tooling disagree with each other. Relying on the SDK's built-in wait would mean a call that goes to voicemail or isn't answered silently polls for the full default timeout (10 minutes) before giving up. `calle/run.py::call_and_wait()` does its own polling against the fuller, case-insensitive set instead of trusting either single convention. (The wider set isn't itself confirmed against the SDK's own generated models, which only define `canceled/completed/failed/in_progress/queued` — the extra statuses come from CLI docs, not the SDK's source. Harmless either way: an unrecognized status is just never matched, not mishandled.)

## A naming collision, and why the repo runs the way it does

`calle-ai` installs an importable package literally named `calle`. This app's own CALL-E adapter is *also* a folder named `calle/`. Running anything with `signcall/` itself as the working directory/import root makes Python resolve `import calle` to the **local** adapter, silently shadowing the real SDK — this actually happened during development and produced no error, just silently wrong behavior, until caught by checking `calle.__file__`.

The fix didn't require renaming anything — it only required running the app from the directory that CONTAINS `signcall/` (not from inside it), so `signcall/calle/` is never itself exposed as a top-level `calle` on `sys.path`:

```bash
cd apps/web/asl_accessibility     # NOT apps/web/asl_accessibility/signcall/
python3 -m signcall.frontend.text_harness clinic_search
```

Internal cross-references inside the app use relative imports (`from ..calle.run import call_and_wait`), so `calle` (bare, absolute) unambiguously means the real SDK everywhere in this codebase.

## Scoped to the US, and currently to two states

Phone numbers, clinic/interpreter directory lookups, and the cancellation-fee research this design is built on (2-hour interpreter minimums, ASL-specific licensure) are all US-specific. Concretely:

- Phone numbers are E.164 with a `+1` country code (see the test fixtures in `frontend/text_harness.py`).
- **This build covers Nevada and Illinois, nothing else.** `workflow/user_input.py::COVERED_STATES` lists one ZIP-centroid file per covered state (`data/nv_zip_centroids.json`, `data/il_zip_centroids.json`), and rejects a ZIP outside both up front with an explicit message rather than letting a run fail several calls deep. Clinic search itself isn't actually state-limited — Apify's Google Maps Scraper works for any US ZIP — the real constraint is the ZIP-to-lat/lon distance table used to rank results and to geocode the Apify search query correctly (see "A real, found bug" below). Widening clinic coverage to another state means adding a centroid file and registering it in `COVERED_STATES` — no other code change.
- **Interpreter sourcing is real only for Illinois, and that's a separate, independent limit from the ZIP gate above.** See "What's real vs. stubbed" below for the full picture; the short version is that a Nevada (or any non-Illinois) clinic's freelance-interpreter search comes back honestly empty rather than fabricating a match, and a real deployer adds a state by wiring a new source into `appointment.py::_resolve_freelance_pool()`, independent of whether that state's ZIPs are in `COVERED_STATES` for distance purposes.
- CALL-E itself supports many more countries, so nothing here is a CALL-E limitation — it's a deliberate scope cut to keep the build tractable, not a technical ceiling.

### A real, found bug: a hardcoded state silently broke every non-Nevada search

`clinic_lookup.py`'s Apify query builder used to hardcode `f"{zipcode}, NV, United States"` as the location to geocode, a leftover from when this build was Nevada-only. The first time this app was tested against a real Illinois ZIP, Apify's geocoder was asked to resolve a nonsense location like "60616, NV, United States," found nothing, and the search returned zero clinics — which (before the fix below) silently fell through to a Nevada-flavored synthetic fallback, so the user got a fake Las Vegas clinic and a fake Nevada interpreter back for a real Chicago request, with no indication anything had gone wrong. Fixed by resolving the ZIP's actual state dynamically (`workflow/user_input.py::zip_state()`, built off the same per-state ZIP data already used for distance) instead of hardcoding one. Verified live against the real Apify API and the real Illinois interpreter directory afterward.

## No synthetic fallback, anywhere, in production

An earlier version of this app degraded to committed synthetic datasets whenever a live lookup failed or came back empty — `data/clinics_fallback.json` for clinics, and `data/interpreters_nv.json`/`workflow/interpreter_matching.py::load_candidates_within_radius()` for interpreters outside Illinois — "to keep a demo alive." **Both are gone from the live call path.** The reasoning: a fabricated clinic or interpreter that *looks like* a real, callable result is a worse failure mode than an honest error, because the person using this app has no way to tell the difference — a fake "booked" appointment at a clinic that doesn't exist is strictly worse than being told the search failed. Concretely, today:

- `clinic_lookup.find_clinics()` returns an empty list on any failure (missing token, Apify error, zero places surviving the filters). `appointment.py` turns that into `"No {type} clinic could be found near {zip} -- nothing to call."` before a single call is placed.
- `appointment.py::_resolve_freelance_pool()` returns an empty list for any ZIP that isn't Illinois's. The same error path turns that into `"No interpreter could be found for {clinic} ({zip}) -- nobody was called, and no clinic appointment was taken."` This only affects the **freelance-interpreter** search specifically — a user who already has their own interpreter, or who has a family member who can interpret, never reaches this code at all, since neither of those paths needs a sourced freelancer.
- `data/interpreters_nv.json` and `load_candidates_within_radius()` still exist, but only as a **test fixture** for the batching/radius/rate-ranking algorithm in `interpreter_matching.py` (see `scenario_roster_load`) — nothing in a live run reads from that file anymore.

## Repo layout

```
apps/web/asl_accessibility/
├── asl-recognition/             ← the actual frontend (separate app, own README) --
│                                   camera + typed-text capture, profile/family storage,
│                                   booking history, POSTs to this app's `POST /runs`
└── signcall/                    ← this app
    ├── __init__.py                 makes `signcall` a real package (see naming-collision note above)
    ├── schemas/
    │   ├── user_input.schema.json  ← THE input contract, validated on every run
    │   └── clinic_record.schema.json   what a clinic is: name, type, phone, zipcode
    ├── data/                    ← committed lookup data (all of it inspectable)
    │   ├── interpreters_nv.json     29 SYNTHETIC interpreters, TEST FIXTURE ONLY --
    │   │                            nothing in a live run reads this file (see above)
    │   ├── nv_zip_centroids.json    NV ZIP → lat/lon (GeoNames, CC BY 4.0) for distances
    │   └── il_zip_centroids.json    IL ZIP → lat/lon/county (GeoNames, CC BY 4.0) --
    │                                no interpreter data here; see workflow/interpreter_lookup.py
    │                                (plus clinics_last_search.json at runtime -- gitignored; an
    │                                inspection dump of the most recent real search, holds real
    │                                business names/numbers when Apify is configured)
    ├── api/                     ← the HTTP seam the frontend POSTs to
    │   ├── server.py                POST /runs, GET /runs/{id}, POST /runs/{id}/confirm,
    │   │                            POST /runs/{id}/cancel, GET /health
    │   ├── notify.py                pushes run events to the frontend's /notifications
    │   └── demo_mocks.py            per-run scripted CALL-E answers, opt-in, zero real calls
    ├── workflow/                ← the demonstrated use case (Interpreter Mesh, fully working)
    │   ├── types.py                 shared dataclasses — UserInput is what everything runs on
    │   ├── user_input.py            schema validation + parsing ("2PM" → a 14:00-15:00 window);
    │   │                            COVERED_STATES, zip_centroids(), zip_state()
    │   ├── calendar.py              Step 2's join math (pure functions, no CALL-E dependency)
    │   ├── confirmation.py          BookingProposal / BookingDeclined — the user's go/no-go contract
    │   ├── clinic_lookup.py         Apify Google Maps Scraper → up to 10 nearby clinics (no CALL-E)
    │   ├── clinic_call.py           Step 2 one-at-a-time insurance-gated search + Step 4 booking
    │   ├── family_call.py           Step 3A the ordered, call-only family list
    │   ├── interpreter_lookup.py    Step 3B's LIVE source: Illinois's own public interpreter
    │   │                            registry (IDHHC), queried per run, never cached
    │   ├── interpreter_matching.py  Step 3B's batching/radius/rate-ranking logic + binding confirm;
    │   │                            load_candidates_within_radius() is a TEST FIXTURE ONLY now
    │   ├── reminders.py             non-CALL-E notifications (confirmation text — stubbed)
    │   └── appointment.py           the orchestrator — Steps 2-4, own-interpreter + full search +
    │                                user-initiated cancellation
    ├── calle/                   ← CALL-E adapter, real SDK wired in, with a mock mode
    │   ├── run.py                   call_and_wait() — the only function that reaches CALL-E;
    │   │                            also real-mode line routing (DEMO_TEST_LINES)
    │   └── result.py                small CallResult helpers
    ├── frontend/
    │   ├── text_harness.py      ← the test suite, text-input shaped — see below
    │   └── live_smoke_test.py   ← standalone real-call diagnostic, NOT part of the workflow —
    │                               places one real call, prints raw API response next to the
    │                               mapped CallResult; this is what actually exercises
    │                               calle/run.py::_map_result() (the mock scenarios don't)
    └── README.md                ← this file
```

## Setup

`calle-ai` requires Python >= 3.11.

```bash
cd apps/web/asl_accessibility/signcall
python3 --version   # confirm this says >= 3.11 before proceeding
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # then edit .env and paste your real key(s) after the "="
```

**Credential handling:** credentials live in `apps/web/asl_accessibility/signcall/.env` (gitignored — never committed) and are loaded automatically by `signcall/__init__.py` via `python-dotenv`, once, for every entry point. `.env.example` is the only version that's committed, and it never contains a real value.

| Variable | Read by | Needed for |
|---|---|---|
| `CALLE_API_KEY` | `calle/run.py` | placing real calls |
| `APIFY_API_TOKEN` | `workflow/clinic_lookup.py` | finding clinics (Google Maps Scraper). No credential-free fallback exists — see "No synthetic fallback" above |
| `SIGNCALL_DISABLE_IL_LOOKUP` | `workflow/interpreter_lookup.py` | optional; skips the live Illinois registry query for offline dev (the test harness sets this automatically where it matters) |

Apify meters **per place scraped**, not per query — one clinic search requests up to 20 places. So the failure mode is a mid-run 402 once credit runs out rather than a clean daily quota, and a run takes tens of seconds because the actor crawls Maps live. A failure comes back as an honest empty result, not a substitute.

Mock mode (default in the test harness) needs none of the above — only real calls do.

## Test the pipeline with typed text (no ASL model needed)

This is the concrete answer to "can the rest of the workflow be built and tested now, assuming text input, while the model is finished separately" — yes:

```bash
cd apps/web/asl_accessibility
python3 -m signcall.frontend.text_harness clinic_search          # the full sequence:
                                                                    # searched clinics → insurance
                                                                    # gate → family → freelance
                                                                    # batch → book → both texts
python3 -m signcall.frontend.text_harness shortcut               # has_interpreter: true —
                                                                    # same clinic search, nobody
                                                                    # called or texted for the
                                                                    # user's own interpreter
python3 -m signcall.frontend.text_harness decline_then_retry       # cheapest-by-rate declines the
                                                                    # binding confirm; the next
                                                                    # match in that SAME batch
                                                                    # takes it
python3 -m signcall.frontend.text_harness cheapest_wrong_day       # the confirmed interpreter is
                                                                    # booked for the slot SHE
                                                                    # named, never another
python3 -m signcall.frontend.text_harness family_locks_in          # family called one at a time
                                                                    # AFTER the clinic match, stops
                                                                    # at the first yes, then gets
                                                                    # texted the outcome
python3 -m signcall.frontend.text_harness family_confirm_fallback  # every family member declines
                                                                    # the binding confirm; a
                                                                    # freelance interpreter covers
                                                                    # the already-booked slot instead
python3 -m signcall.frontend.text_harness clinic_search_exhausted  # the 10-clinic ceiling: 11
                                                                    # offered, exactly 10 called
python3 -m signcall.frontend.text_harness requirements             # requirements/notes from the
                                                                    # calls surface in the proposal
python3 -m signcall.frontend.text_harness clinic_alternate_date_rejected  # a clinic's booking call
                                                                    # reporting a DIFFERENT date/time
                                                                    # than asked for is rejected,
                                                                    # not silently accepted
python3 -m signcall.frontend.text_harness freelance_parallel       # SIGNCALL_FREELANCE_PARALLEL_BATCH=1:
                                                                    # a batch's members are dialled
                                                                    # at once, not one after another
python3 -m signcall.frontend.text_harness sequential_batch_calls_both  # the default (sequential)
                                                                    # dispatch still asks every
                                                                    # batch member, nobody skipped
python3 -m signcall.frontend.text_harness declined                 # the user says no: nothing
                                                                    # booked, run ends cleanly
python3 -m signcall.frontend.text_harness interpreters_all_decline # every interpreter declines
                                                                    # the final confirm; the clinic
                                                                    # booking is cancelled
python3 -m signcall.frontend.text_harness input_validation         # every malformed profile
                                                                    # rejected BEFORE any call
python3 -m signcall.frontend.text_harness clinic_lookup            # the Maps parser against
                                                                    # canned Apify items (no
                                                                    # network), plus the honest
                                                                    # empty result on a missing
                                                                    # token -- no synthetic fallback
python3 -m signcall.frontend.text_harness roster_load              # the interpreter-matching
                                                                    # algorithm's TEST FIXTURE: 29
                                                                    # synthetic records, radius-
                                                                    # bounded, nearest-first
python3 -m signcall.frontend.text_harness il_interpreter_lookup    # Illinois ZIP/county lookup,
                                                                    # phone extraction, Active+has-
                                                                    # a-phone filtering, county-
                                                                    # then-region tiering -- against
                                                                    # canned directory rows, no
                                                                    # network
python3 -m signcall.frontend.text_harness call_routing             # real-mode dialling resolves
                                                                    # ONLY to the owned test
                                                                    # line(s) -- see DEMO_TEST_LINES
                                                                    # below
python3 -m signcall.frontend.text_harness api_endpoint            # the HTTP endpoint in-process:
                                                                    # 400 / 202 / confirm / cancel /
                                                                    # 409 / 404, booking history's
                                                                    # "forgotten run" adoption path
```

All nineteen are verified working end-to-end, entirely in `CALLE_MOCK_MODE=1` and with no network (set automatically by the harness) — every phone number's response is scripted via `calle.run.register_mock()`, so you can invent new scenarios (all-decline, rare reversal) by registering different mock responses without touching `workflow/` at all. Mock resolvers branch on the **task text** as well as the phone number, because the winning clinic is now called twice per run (search, then book) and an interpreter up to twice (availability, then binding confirm).

**To place a real call**, unset mock mode:
```bash
CALLE_MOCK_MODE=0 python3 -m signcall.frontend.text_harness <scenario>
```
**You cannot point this at a real clinic, and that is enforced, not advised.** In real mode `calle/run.py::resolve_dial_target()` maps every recipient onto one of the test lines in `DEMO_TEST_LINES`, owned by this project's operator, assigned by the recipient's position within the batch and kept stable per recipient — so the clinic searched on line 2 is booked on line 2. (`DEMO_TEST_LINES` currently holds the same number three times over, by deliberate choice, for solo live-testing on one phone rather than needing three to monitor at once — this is why `scenario_call_routing`'s "different lines don't share a lock" assertion currently fails; that's expected under this configuration, not a bug to chase.) The logical recipient still drives the task text, the mock lookup and the evidence trail; only the number handed to the SDK is substituted, and it is never the logical one. `frontend/live_smoke_test.py` bypasses `call_and_wait()` by design, so it enforces the same rule itself and refuses any number outside that list.

Clinic phone numbers found by `clinic_lookup.py` are **real**, and so are interpreter numbers found by `interpreter_lookup.py` for an Illinois clinic. Neither is ever actually dialled in real mode — both are redirected onto a `DEMO_TEST_LINES` entry regardless of source.

## Run the API

```bash
cd apps/web/asl_accessibility
CALLE_MOCK_MODE=1 SIGNCALL_API_DEMO_MOCKS=1 \
  python3 -m uvicorn signcall.api.server:app --host 127.0.0.1 --port 8000
```

**The server refuses to start in real-call mode unless you say so twice.** `CALLE_MOCK_MODE` defaults to `0` and `.env` can hold a working `CALLE_API_KEY`, so a bare launch would come up ready to dial — and because submission already consents to the search (see "The consent gate" above), a single POST from an open browser tab would place real search calls with no further click. Either set `CALLE_MOCK_MODE=1` (safe) or `SIGNCALL_API_ALLOW_REAL_CALLS=1` (deliberate). `SIGNCALL_API_DEMO_MOCKS=1` scripts a plausible run from the submitted profile so the whole cycle works with zero calls.

| Route | Request | Response |
|---|---|---|
| `POST /runs` | the user-input JSON, exactly as `schemas/user_input.schema.json` defines it | **202** `{run_id, status, mock_mode, plan}` · **400** with the validator's own message if the profile is malformed (nothing is dialled) · **409** if a run is already in progress |
| `GET /runs/{run_id}` | — | `{status: running\|awaiting_confirmation\|succeeded\|declined\|failed\|cancelling\|cancelled\|cancel_failed, proposal, result, error, error_type, plan, started_at, finished_at}` · **404** for an unknown id |
| `POST /runs/{run_id}/confirm` | `{"approved": true\|false}` | **200** once the answer is recorded · **409** if the run isn't waiting for one (including a second answer) · **404** for an unknown id |
| `POST /runs/{run_id}/cancel` | optional: `{"result": {...}, "patient_name": "..."}` — only needed when this process no longer remembers the run (a restart between booking and cancelling); the caller supplies the booking's own already-stored `result` and the patient's name, verbatim | **202** `{status: "cancelling"}` · **409** if the run isn't in a cancellable state (including cancelling twice) · **404** if the run is unknown AND no body was supplied to adopt it from |
| `GET /health` | — | mode flags and whether credentials are present (never their values) |

A run is accepted, not awaited: a real one places many calls over several minutes, so the frontend polls `GET /runs/{run_id}`. Errors inside the run land as `status: "failed"` with the message and exception type; nothing 500s after acceptance. This server keeps **no durable storage of its own** — `_runs` is a plain in-memory dict, gone the moment the process restarts — which is exactly why `POST .../cancel`'s body is optional: the frontend's own database (see `../asl-recognition/db.py`) outlives this process by design, and a cancel request for a run this process has forgotten "adopts" it from the caller-supplied body instead of requiring its own memory.

**One run at a time**, enforced with a lock — a second POST gets 409 naming the active run. This isn't politeness: `calle/run.py`'s line-assignment map is module-level and gets reset at the start of every run, so two overlapping runs would erase each other's test-line assignments and the booking call would fail *after* an interpreter had already committed.

Things to know before pointing anything else at it:

- **No authentication.** Single user, browser on localhost — this is a disclosed, deliberate scope cut for this contribution tier, not an oversight. CORS is restricted to `http://localhost:*` / `http://127.0.0.1:*` origins, but **CORS is not a security boundary** — it stops another site's JavaScript reading responses, not curl, an extension, or any local process from POSTing and triggering calls. A `file://`-served frontend sends `Origin: null` and will be blocked.
- **Don't pass `--workers >1` or `--reload`.** Each worker gets its own lock and registry, which breaks the one-run invariant. `--host 0.0.0.0` puts an unauthenticated call-placing endpoint on your network.
- **`GET /runs/{id}` returns the plan text**, which includes the patient's name, date of birth and insurance policy number, to anything that can reach the port. The run registry is in-memory and unbounded — restarting forgets everything, including an in-flight run.
- **Phone numbers are intentionally real and unmasked in every API response** (`clinic_contact.phone`, `interpreter.phone`) — this was a deliberate decision, not an oversight: the whole point of this app is placing real calls, and the cancellation feature depends on the frontend having the real number to call back if an automated cancellation ever fails. Masking is applied in real-mode *dialling* (see `DEMO_TEST_LINES` above), never in the data the app itself operates on.

## What's real vs. stubbed right now

| Piece | Status |
|---|---|
| Step 2–4 sequence, own-interpreter branch | **Real, tested** — `workflow/appointment.py` |
| User-input contract: schema validation, one-hour slot parsing, weekday/date and past-date checks | **Real, tested** — `schemas/user_input.schema.json` + `workflow/user_input.py` |
| Clinic discovery from ZIP + appointment type | **Real** — `workflow/clinic_lookup.py` (Apify Google Maps Scraper, any US ZIP; parser tested against canned dataset items, live path needs `APIFY_API_TOKEN`). No synthetic fallback — see "No synthetic fallback" above |
| Clinic record contract | **Real, tested** — `schemas/clinic_record.schema.json`: name, type, phone, zipcode, validated per record |
| Real-mode call routing onto the owned test line(s) | **Real, tested** — `calle/run.py::resolve_dial_target()` |
| Clinic search: insurance gate, one clinic at a time, nearest-first, stop at the first match | **Real, tested** — `workflow/clinic_call.py::search_clinics()` |
| Calendar intersection (clinic slots ∩ user availability) | **Real, tested** — `workflow/calendar.py::matched_slots()` |
| Family as an ordered, call-only list checked after the clinic match | **Real, tested** — `workflow/family_call.py` |
| Freelance batches of `DEFAULT_BATCH_SIZE` (2), cheapest-by-rate within the batch, non-binding ask then one binding confirm, decline→retry inside that batch. Dialled one after another by default (`PARALLEL_BATCH_CALLS`/`SIGNCALL_FREELANCE_PARALLEL_BATCH` off) since a shared CALL-E line only runs one call at a time; both batch members are still always asked either way | **Real, tested** — `workflow/interpreter_matching.py` |
| Clinic booking-success verification | **Real, tested** — `workflow/clinic_call.py::_require_booked()`; note the commit ordering it used to protect has been deliberately traded away (see Known limitations) |
| CALL-E adapter, real SDK calls, mock mode | **Real, exercised against a live account** — found and fixed real response-mapping and routing bugs; see `calle/run.py` |
| Interpreter sourcing | **Real for Illinois, honest empty result everywhere else covered** — an Illinois clinic's ZIP routes to `workflow/interpreter_lookup.py::find_interpreters_il()`, a live, uncached, per-run query against IDHHC's (Illinois Deaf and Hard of Hearing Commission) own public licensed-interpreter directory, filtered to an Active license and a published phone number, matched by county then region (no ZIP/address is published per interpreter). A live scrape of RID's (Registry of Interpreters for the Deaf) registry was tried first and removed: RID's own terms don't permit automated scraping of that site, and its robots.txt backs that up; IDHHC's carries no such restriction and its own page offers a CSV export, which is some of the evidence for that (see that module's docstring for the rest). Everywhere else covered, the freelance search comes back empty rather than substituting a synthetic match — see "No synthetic fallback" above. A real deployer with a consented interpreter source for another state adds it the same way, at `appointment.py::_resolve_freelance_pool()` |
| Final confirmation text to the user and the secured interpreter | **Composed for real, delivery stubbed** — `reminders.send_confirmation_text()` raises; `appointment.py` catches it and records the exact undelivered message in `evidence` rather than failing a run whose appointment is genuinely booked |
| User-initiated cancellation of a succeeded booking | **Real, tested** — `POST /runs/{id}/cancel`; `workflow/appointment.py::cancel_appointment()` calls the clinic to cancel, then releases whoever was interpreting (family or freelance; nobody for a user-arranged interpreter). Retriable from `cancel_failed`. Works from a stored result alone — no live objects from the original run needed, and no durable storage in this process either (see "Run the API" above) |
| Adding the booking to the user's calendar | **Not implemented** — the design names no mechanism, so none was invented |
| ASL recognizer / frontend | **Built, in `../asl-recognition/`** — camera and typed-text capture, profile + family storage, booking submission and polling, booking-history list with per-booking cancel, all POSTing to this app. See that directory's own README for its scope |
| HTTP endpoint for the frontend | **Real, tested** — `api/server.py`; validated in-process by the `api_endpoint` scenario and against a live uvicorn server |

## Known limitations

Listed here rather than silently left out, matching this project's own disclosure standard. None of these crash; they're documented gaps.

**Scope decisions:**

- **No authentication.** See "Run the API" above.
- **Two states covered, interpreter sourcing real for one of them.** See "Scoped to the US" above.
- **The appropriateness gate is gone.** Earlier versions classified a visit as routine vs. complex/sensitive and refused to consider family for the latter. The agent no longer classifies anything: the user decides via `has_interpreter` and their own family list.
- **The booking call identifies the patient.** Name, date of birth, age, phone and insurance provider + policy number all go to the clinic that ends up holding the appointment — a real clinic can't book an anonymous slot. The clinic *search* calls still say nothing about the patient, and `describe_goal()` discloses the whole thing in the consent text before anything is dialled.
- **`has_interpreter: true` collects nothing about that interpreter.** No name, no number, no availability — so clinic slots are matched against the user's own windows exactly as on the full path, one matching slot is picked at random, and nobody is called or texted on that interpreter's behalf. The result carries `{"tier": "user_arranged"}` with no name.
- **The asymmetric-commit safety property has been traded away, knowingly.** The finalized sequence confirms an interpreter *before* the clinic is called back to book for real — if that booking call then fails, a fee-bearing interpreter engagement exists with no appointment behind it, and nothing releases them automatically. `_require_booked()`'s error names the interpreter and says the user must phone them directly.

**Known gaps in the current implementation:**

- **Clinic search yield isn't guaranteed.** Places are dropped for: no callable US number, no 5-digit ZIP, closed, non-US, a duplicate phone (multi-location practices share one central number), or a category that doesn't plausibly match what was searched for. 20 places are requested to fill a list of 10. With zero survivors, a missing token, or an Apify error, the search returns an honest empty list — no synthetic fallback (see above).
- **The category filter is a keyword allow-list, not semantics.** Maps returns adjacent and sponsored businesses (a sunglasses shop for "optometrist"), and nothing downstream reads the clinic's category — the CALL-E task text is built from the *user's* requested appointment type — so an off-category listing would be phoned with the wrong script. `CATEGORY_KEYWORDS` in `clinic_lookup.py` is the guard; widen it if a legitimate clinic type gets filtered out.
- **"Nearest-first" is ZIP-granular, and discards Maps' own ordering.** Distance comes from ZIP centroids, so every clinic sharing the user's ZIP scores 0.0, and a ZIP outside the covered-states centroid table sorts *last*. Carrying each place's coordinates would fix this; the record is deliberately four fields.
- **A clinic search takes tens of seconds and blocks.** `find_clinics()` starts an Apify actor run and polls it (3s interval, 240s deadline) inside the synchronous workflow.
- **Illinois interpreter matching has no ZIP-radius equivalent.** IDHHC's directory publishes city/county/region per interpreter, not a ZIP or address, so matching is by county (then region as a second tier) rather than a mile radius. This is a real data ceiling, not a code shortcut — there's nothing more precise to compute from.
- **Satisficing, not optimizing — by design.** "Nearest" and "cheapest" are only ever compared *within* the batch that first matched, so two runs against the same clinic/interpreter pool can book different people depending on batch order.
- **A freelance candidate who is available but gives no hourly rate is not counted as a batch match**, and the search continues to the next batch. This keeps an unpriced candidate from reaching the binding confirm call (which would read "$None/hr" to a real person), at the cost of a genuinely available responder not stopping the search.
- **`rank_by_cost()` drops a candidate with no stated minimum-hours** (`minimum_hours: null` reads as "can't compute cost," not "no minimum applies").
- **RARE REVERSAL isn't wired up.** `interpreter_matching.send_release()` exists and works but nothing calls it — there's no code path today that notices a clinic booking got cancelled (by the clinic, not the user) after an interpreter already confirmed.
- **Family and freelance matching depend on exact slot-string equality** (`"2026-09-24 14:00"`). A real call answering `"14:00:00"`, `"2026-09-24T14:00"`, or `"Thu 2pm"` silently yields no match and reports the search exhausted.
- **Clinic-returned date/time strings aren't validated or normalized** before parsing — a transcript returning "Sept 24th" instead of "2026-09-24" raises after the (real, paid) search call has already happened. The *user's* side is validated hard; the clinics answering are real and un-normalized.
- **`AppointmentResult`'s shape drifts from the original design doc's** in a few fields: no booked date/time survives into the top-level result on any path (it's under `appointment_slot` instead), and `cancellation_deadline` is always `None`.
- **The run mutates caller-supplied objects** — `ClinicCandidate`, `FamilyInterpreter`, and `InterpreterCandidate` all have their answers written back in place, so re-running with the same objects starts from a dirty state. A caller who hand-builds a list and reuses it across runs gets stale answers the second time; the roster-loading test fixture sidesteps this by building fresh candidates per call.
- **`interpreter_matching.rank_by_cost()` and `TimeWindow.intersection()` are unreferenced, kept deliberately** — the first is the documented tie-break by hourly rate, the second's only caller was an earlier constraint calculation this design no longer uses.

## Next steps, roughly in order

1. Pick an SMS/email/push provider for `reminders.send_confirmation_text()`, and decide whether the calendar write is worth adding.
2. Extend interpreter sourcing to another state: find a public, automation-permitting directory for it (see `workflow/interpreter_lookup.py`'s docstring for what made Illinois's usable and RID's not), then wire it into `appointment.py::_resolve_freelance_pool()` the same way.
3. Widen clinic/distance coverage to more states: add a `data/<state>_zip_centroids.json` (same GeoNames export, filtered differently) and register it in `workflow/user_input.py::COVERED_STATES` — clinic search itself needs no change.
4. Consider masking or truncating real phone numbers specifically in anything meant for a screen recording or a public demo video — the live API/data itself stays unmasked by design (see "Run the API" above), but a recording is a different audience than the app's own operator.
5. When a UI need for it appears, surface `describe_goal()`'s full consent text progressively rather than as the only copy shown — `../asl-recognition/`'s current screens already summarize it to one line by default, with the full text available on request.
