# signcall

An ASL accessibility layer for CALL-E: sign your intent, the agent phones the hearing world to book the appointment. Built for the *CALL-E: Your Code Is Calling* hackathon. Full problem statement, evidence base, and the finalized workflow spec live in `../../CALL-E Hackathon Ideas.md` (Idea 2, Interpreter Mesh) — this repo is the implementation of that spec.

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

Worth being precise about the seam, because it's easy to overstate: a sign-language recognizer emits *gloss*, and gloss cannot plausibly carry a policy number, a date of birth, or a family member's phone number. So the JSON is what the **agent** consumes, and it's assembled from two sources — the part a user expresses per visit (appointment type, availability, whether they already have an interpreter) and the part that lives in a registered profile (identity, ZIP, insurance, the family list). Which half comes from signing, typing, or storage is deliberately outside this repo's scope. `frontend/text_harness.py` builds the whole object directly.

## CALL-E integration: SDK only

This app uses the **`calle-ai` Python SDK exclusively**. Nothing in this codebase invokes the `calle` CLI, MCP tools, or raw REST — one integration path, no mixing. (An earlier pass through this project briefly installed the CLI and its agent skill while exploring the CALL-E ecosystem; both have since been fully uninstalled — global npm package removed, skill removed, OAuth session logged out, local cache cleared. They played no role in this app's code and are gone.)

**Why the SDK and not the alternatives**, briefly:
- MCP's `plan_call`/`run_call`/`get_call_run` tools exist for an LLM deciding what to do at each conversational turn. This workflow is a fully-specified deterministic state machine instead (family match? proximity? cost rank? yes/no on a confirm call?) — none of that needs an LLM in the loop, so MCP would add latency and non-determinism for nothing.
- The CLI + its agent skill are built for a *chat-based AI agent* placing calls on a human's behalf mid-conversation, which is a different threat model (verifying binary identity before every shell-out, treating all output as untrusted) than *this app's own trusted internal code* calling a pinned dependency.
- The SDK is what a backend service should call directly — a typed, importable client, not a subprocess wrapper — and reads as more idiomatic "skillful CALL-E usage" (a named judging criterion) for other contributors extending this app.

**One credential, one place it's used:** a static API key (`Authorization: Bearer`) from your CALL-E account dashboard, set as `CALLE_API_KEY` and read only by `calle/run.py`. Nothing else in this repo needs, stores, or reads any CALL-E credential.

### The consent gate is pure application logic, not a CALL-E feature

An earlier draft of this code assumed CALL-E exposed a two-step "plan without dialing, then confirm, then run" flow at the SDK/REST level, mirroring the MCP tool names. **Direct inspection of the installed SDK's source (`calle/calls.py`) shows that's wrong** — `client.calls.create()` immediately creates *and dispatches* the call; there's no "plan-only" endpoint outside MCP. The fix: `calle/plan.py::create_plan()` makes zero network calls — it's local logic that builds the goal text and hands it back for the user to approve. Nothing reaches CALL-E until `workflow/appointment.py` has already rendered the plan and gotten an explicit yes. This is arguably a *stronger* accessibility property than the MCP-level gate would have given: literally nothing touches CALL-E before the Deaf user approves, not even a draft.

### A correctness gap found by reading the SDK's actual code, not its docs

The SDK's own `wait_for_result()` only treats `{"completed", "failed", "canceled"}` as terminal (lowercase). CALL-E's own CLI documentation lists a much wider terminal set — `COMPLETED`, `FAILED`, `NO_ANSWER`, `DECLINED`, `CANCELED`, `CANCELLED`, `VOICEMAIL`, `BUSY`, `EXPIRED` (uppercase). These two pieces of official CALL-E tooling disagree with each other. Relying on the SDK's built-in wait would mean a call that goes to voicemail or isn't answered silently polls for the full default timeout (10 minutes) before giving up. `calle/run.py::call_and_wait()` does its own polling against the fuller, case-insensitive set instead of trusting either single convention.

## A naming collision, and why the repo runs the way it does

`calle-ai` installs an importable package literally named `calle`. This app's own CALL-E adapter is *also* a folder named `calle/` (matching the diagram this repo was built from). Running anything with `apps/signcall/` itself as the working directory/import root makes Python resolve `import calle` to the **local** adapter, silently shadowing the real SDK — this actually happened during development and produced no error, just silently wrong behavior, until caught by checking `calle.__file__`.

The fix didn't require renaming anything — it only required running the app one level up, so `apps/signcall/calle/` is never itself exposed as a top-level `calle` on `sys.path`:

```bash
cd apps/                          # NOT apps/signcall/
python3 -m signcall.frontend.text_harness clinic_search
```

Internal cross-references inside the app use relative imports (`from ..calle.run import call_and_wait`), so `calle` (bare, absolute) unambiguously means the real SDK everywhere in this codebase.

## Scoped to the US for now

Phone numbers, clinic/interpreter directory lookups, and the cancellation-fee research this design is built on (2-hour interpreter minimums, ASL-specific licensure) are all US-specific. Concretely:

- Phone numbers are E.164 with a `+1` country code (see the test fixtures in `frontend/text_harness.py`).
- Tighter than US-wide, in fact: **this build is scoped to Nevada.** `data/nv_zip_centroids.json` (distances) and `data/interpreters_nv.json` (the roster) both cover NV only, and `workflow/user_input.py` rejects a non-NV ZIP up front with an explicit message rather than letting the run fail several calls deep. Widening it means adding centroid rows and roster records — no code change.
- CALL-E itself supports many more countries, so nothing here is a CALL-E limitation — it's a deliberate scope cut to keep the build tractable, not a technical ceiling.

## Repo layout

```
apps/signcall/
├── __init__.py                 ← makes `signcall` a real package (see naming-collision note above)
├── asl/                        ← reusable accessibility layer (being built separately —
│   ├── recognizer/               folders below intentionally not scaffolded yet; this
│   ├── vocabulary/                README documents the CONTRACT they must satisfy)
│   ├── synonyms/
│   └── fingerspelling/
├── schemas/
│   ├── user_input.schema.json  ← THE input contract, validated on every run
│   └── clinic_record.schema.json   what a clinic is: name, type, phone, zipcode
├── data/                       ← committed lookup data (all of it inspectable)
│   ├── interpreters_nv.json        30 SYNTHETIC interpreters, 15 of them in Las Vegas
│   ├── nv_zip_centroids.json       NV ZIP → lat/lon (GeoNames, CC BY 4.0) for distances
│   └── clinics_fallback.json       synthetic emergency fallback if the clinic search yields nothing
│                                   (plus clinics_last_search.json at runtime — gitignored)
├── api/                        ← the HTTP seam the frontend POSTs to
│   ├── server.py                   POST /runs, GET /runs/{id}, GET /health
│   └── demo_mocks.py               per-run scripted answers, opt-in, zero real calls
├── workflow/                   ← the demonstrated use case (Interpreter Mesh, fully working)
│   ├── types.py                    shared dataclasses — UserInput is what everything runs on
│   ├── user_input.py               schema validation + parsing ("2PM" → a 14:00-15:00 window)
│   ├── calendar.py                 Step 2's join math (pure functions, no CALL-E dependency)
│   ├── clinic_lookup.py            Apify Google Maps Scraper → up to 10 nearby clinics (no CALL-E)
│   ├── clinic_call.py              Step 2 batched insurance-gated search + Step 4 booking
│   ├── family_call.py              Step 3A the ordered, call-only family list
│   ├── interpreter_matching.py     Step 3B roster loading, freelance batches-of-3, binding confirm
│   ├── reminders.py                non-CALL-E notifications (confirmation text — stubbed)
│   └── appointment.py              the orchestrator — Steps 2-4, own-interpreter + full search
├── calle/                      ← CALL-E adapter, real SDK wired in, with a mock mode
│   ├── plan.py                     local-only consent gate (no CALL-E API calls at all)
│   ├── run.py                      call_and_wait() — the only function that reaches CALL-E
│   └── result.py                   small CallResult helpers
├── frontend/
│   ├── text_harness.py         ← minimal ASL-focused UI, TEXT-INPUT VERSION — see below
│   └── live_smoke_test.py      ← standalone real-call diagnostic, NOT part of the workflow —
│                                  places one real call, prints raw API response next to the
│                                  mapped CallResult; this is what actually exercises
│                                  calle/run.py::_map_result() (the mock scenarios don't)
└── README.md                   ← this file
```

### Contract the ASL layer must satisfy (once it's ready to wire in)

`asl/` isn't scaffolded yet because the recognizer is being built with a different approach than originally assumed here, and its exact shape isn't settled. What it has to feed is `schemas/user_input.schema.json` — but only the part of it a person can actually sign: `appointment_type`, `availability`, and `has_interpreter`. The rest (name, date of birth, age, phone, ZIP, insurance, the family list) is registered profile data that no gloss sequence could carry, and pretending otherwise would be the kind of overclaim this README exists to avoid. When the model is ready, it fills its share of the JSON object, the profile fills the rest, and `workflow/user_input.py` validates the result — nothing downstream changes.

## Setup

`calle-ai` requires Python >= 3.11. The Mac this was built on had *four* Python claimants in play — Apple's bundled 3.9 (was winning for scripts/CI), Anaconda's 3.12, a dangling python.org 3.14 PATH entry left by an old installer, and Homebrew's 3.12 (`brew install python@3.12`), which is the one actually used. Getting Homebrew's Python to win **unconditionally** — not just in an interactive Terminal window, which is the easy 80% — took edits to three files, because zsh sources different files for different shell classes and macOS's own `path_helper` reorders PATH in between them:

- `~/.zshenv` — sourced by every zsh invocation, interactive or not, login or not (scripts, CI, git hooks). Without this, non-interactive shells fell through to Apple's 3.9.
- `~/.zprofile` — sourced by login shells, *after* macOS's system-wide `path_helper` has already rebuilt PATH from `/etc/paths` (which puts `/usr/bin` back ahead). Without this, login-non-interactive shells specifically still fell through to Apple's 3.9 even with the `.zshenv` fix in place.
- `~/.zshrc` — sourced by interactive shells, after conda's own init hook re-prepends its base environment. Without this, `conda`'s auto-activation would win back over Homebrew.

All four shell classes (login/non-login × interactive/non-interactive) and both unversioned (`python3`) and versioned (`python3.12`) invocations are verified resolving to Homebrew's 3.12.14. `conda activate <env>` still correctly takes precedence when explicitly invoked — that's intentional, not a gap.

```bash
cd apps/signcall
python3 --version   # confirm this says 3.12.x before proceeding -- if it
                     # doesn't, open a NEW terminal window first (shell
                     # config changes only apply to shells started after
                     # the edit, not ones already open)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # then edit .env and paste your real key after the "="
```

**Credential handling:** credentials live in `apps/signcall/.env` (gitignored — never committed) and are loaded automatically by `calle/__init__.py` via `python-dotenv`, once, for every entry point. `.env.example` is the only version that's committed, and it never contains a real value. Two keys, each read by exactly one module:

| Variable | Read by | Needed for |
|---|---|---|
| `CALLE_API_KEY` | `calle/run.py` | placing real calls |
| `APIFY_API_TOKEN` | `workflow/clinic_lookup.py` | finding clinics (Google Maps Scraper) |

Apify meters **per place scraped**, not per query — one clinic search requests up to 20 places. So the failure mode is a mid-run 402 once credit runs out rather than a clean daily quota, and a run takes tens of seconds because the actor crawls Maps live. Every failure degrades to the committed synthetic fallback instead of killing the run.

Mock mode (default in the test harness) needs none of the above — only real calls do.

## Test the pipeline with typed text (no ASL model needed)

This is the concrete answer to "can the rest of the workflow be built and tested now, assuming text input, while the model is finished separately" — yes:

```bash
cd apps/            # NOT apps/signcall/ -- see the naming-collision note above
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
python3 -m signcall.frontend.text_harness clinic_search_exhausted  # the 10-clinic ceiling: 11
                                                                    # offered, exactly 10 called
python3 -m signcall.frontend.text_harness input_validation         # every malformed profile
                                                                    # rejected BEFORE any call
python3 -m signcall.frontend.text_harness clinic_lookup            # the Maps parser against
                                                                    # canned Apify items — no
                                                                    # network
python3 -m signcall.frontend.text_harness roster_load              # 30 synthetic interpreters,
                                                                    # radius-bounded, nearest-first
python3 -m signcall.frontend.text_harness call_routing             # real-mode dialling resolves
                                                                    # ONLY to the three owned test
                                                                    # lines
python3 -m signcall.frontend.text_harness api_endpoint            # the HTTP endpoint in-process:
                                                                    # 400 / 202 / poll / 409 / 404
```

All eleven are verified working end-to-end, entirely in `CALLE_MOCK_MODE=1` and with no network (set automatically by the harness) — every phone number's response is scripted via `calle.run.register_mock()`, so you can invent new scenarios (all-decline, rare reversal) by registering different mock responses without touching `workflow/` at all. Mock resolvers branch on the **task text** as well as the phone number, because the winning clinic is now called twice per run (search, then book) and an interpreter up to twice (availability, then binding confirm).

**To place a real call**, unset mock mode:
```bash
CALLE_MOCK_MODE=0 python3 -m signcall.frontend.text_harness <scenario>
```
**You cannot point this at a real clinic, and that is enforced, not advised.** In real mode `calle/run.py::resolve_dial_target()` maps every recipient onto one of three test lines owned by this project's operator (`DEMO_TEST_LINES`), assigned by the recipient's position within the batch and kept stable per recipient — so the clinic searched on line 2 is booked on line 2. The logical recipient still drives the task text, the mock lookup and the evidence trail; only the number handed to the SDK is substituted, and it is never the logical one. `frontend/live_smoke_test.py` bypasses `call_and_wait()` by design, so it enforces the same rule itself and refuses any number outside that list.

Clinic phone numbers found by `clinic_lookup.py` are **real**; interpreter numbers are synthetic. Neither is ever dialled.

## Run the API

The frontend is built separately and lives outside this repo. It hands its user-input JSON to this endpoint, which validates it and starts a run.

```bash
cd apps/            # NOT apps/signcall/ -- see the naming-collision note above
source signcall/.venv/bin/activate
CALLE_MOCK_MODE=1 SIGNCALL_API_DEMO_MOCKS=1 \
  python3 -m uvicorn signcall.api.server:app --host 127.0.0.1 --port 8000
```

**The server refuses to start in real-call mode unless you say so twice.** `CALLE_MOCK_MODE` defaults to `0` and `.env` holds a working `CALLE_API_KEY`, so a bare launch would come up ready to dial — and because submission is the consent, a single POST from an open browser tab would spend ~15 real calls with no further click. Either set `CALLE_MOCK_MODE=1` (safe) or `SIGNCALL_API_ALLOW_REAL_CALLS=1` (deliberate). `SIGNCALL_API_DEMO_MOCKS=1` scripts a plausible run from the submitted profile so the whole cycle works with zero calls.

| Route | Request | Response |
|---|---|---|
| `POST /runs` | the user-input JSON, exactly as `schemas/user_input.schema.json` defines it | **202** `{run_id, status, mock_mode, plan}` · **400** with the validator's own message if the profile is malformed (nothing is dialled) · **409** if a run is already in progress |
| `GET /runs/{run_id}` | — | `{status: running\|succeeded\|failed, result, error, error_type, plan, started_at, finished_at}` · **404** for an unknown id |
| `GET /health` | — | mode flags and whether credentials are present (never their values) |

A run is accepted, not awaited: a real one places around fifteen calls over many minutes, so the frontend polls `GET /runs/{run_id}`. Errors inside the run land as `status: "failed"` with the message and exception type; nothing 500s after acceptance.

**One run at a time**, enforced with a lock — a second POST gets 409 naming the active run. This isn't politeness: `calle/run.py`'s line-assignment map is module-level and gets reset at the start of every run, so two overlapping runs would erase each other's test-line assignments and the booking call would fail *after* an interpreter had already committed.

Things to know before pointing anything else at it:

- **No authentication.** Single user, browser on localhost. CORS is restricted to `http://localhost:*` / `http://127.0.0.1:*` origins, but **CORS is not a security boundary** — it stops another site's JavaScript reading responses, not curl, an extension, or any local process from POSTing and triggering calls. A `file://`-served frontend sends `Origin: null` and will be blocked.
- **Don't pass `--workers >1` or `--reload`.** Each worker gets its own lock and registry, which breaks the one-run invariant. `--host 0.0.0.0` puts an unauthenticated call-placing endpoint on your network.
- **`GET /runs/{id}` returns the plan text**, which includes the patient's name, date of birth and insurance policy number, to anything that can reach the port. The run registry is in-memory and unbounded — restarting forgets everything, including an in-flight run.

## What's real vs. stubbed right now

| Piece | Status |
|---|---|
| Step 2–4 sequence, own-interpreter branch | **Real, tested** — `workflow/appointment.py` |
| User-input contract: schema validation, one-hour slot parsing, weekday/date and past-date checks | **Real, tested** — `schemas/user_input.schema.json` + `workflow/user_input.py` |
| Clinic discovery from ZIP + appointment type | **Real** — `workflow/clinic_lookup.py` (Apify Google Maps Scraper; parser tested against canned dataset items, live path needs `APIFY_API_TOKEN`) |
| Clinic record contract | **Real, tested** — `schemas/clinic_record.schema.json`: name, type, phone, zipcode, validated per record |
| Real-mode call routing onto three owned test lines | **Real, tested** — `calle/run.py::resolve_dial_target()` |
| Clinic search: insurance gate, batches of 3, stop-at-first-matching-batch, nearest-within-batch | **Real, tested** — `workflow/clinic_call.py::search_clinics()` |
| Calendar intersection (clinic slots ∩ user availability) | **Real, tested** — `workflow/calendar.py::matched_slots()` |
| Family as an ordered, call-only list checked after the clinic match | **Real, tested** — `workflow/family_call.py` |
| Freelance batches of 3, cheapest-by-rate within the batch, non-binding ask then one binding confirm, decline→retry inside that batch | **Real, tested** — `workflow/interpreter_matching.py` |
| Clinic booking-success verification | **Real, tested** — `workflow/clinic_call.py::_require_booked()`; note the commit ordering it used to protect has been deliberately traded away (see Known limitations) |
| CALL-E adapter, real SDK calls, mock mode | **Real, exercised against a live account twice** (2026-09-13) — found and fixed two response-mapping bugs; see `calle/run.py` |
| Interpreter roster | **Real, tested, and synthetic on purpose** — `data/interpreters_nv.json` via `interpreter_matching.load_candidates_within_radius()`; 30 invented interpreters, 15 in Las Vegas, reserved 555-01xx numbers |
| Final confirmation text to the user and the secured interpreter | **Composed for real, delivery stubbed** — `reminders.send_confirmation_text()` raises; `appointment.py` catches it and records the exact undelivered message in `evidence` rather than failing a run whose appointment is genuinely booked |
| Adding the booking to the user's calendar | **Not implemented** — the design names no mechanism, so none was invented |
| ASL recognizer | **Not started here** — being built separately; contract documented above |
| HTTP endpoint for the frontend | **Real, tested** — `api/server.py`; validated in-process by the `api_endpoint` scenario and against a live uvicorn server |
| Frontend capture UI | **Built separately, outside this repo** — it POSTs to `/runs`; `frontend/text_harness.py` remains the text-input path for testing |

## Known limitations

Listed here rather than silently left out, matching this project's own disclosure standard. None of these crash; they're documented gaps between the finalized doc and the current code.

**Introduced deliberately by the 2026-09-13 finalized workflow** (decisions, not defects):

- **The appropriateness gate is gone.** Earlier versions classified a visit as routine vs. complex/sensitive and refused to consider family for the latter. The agent no longer classifies anything: the user decides via `has_interpreter` and their own family list. The research on family-interpreter error rates stays in the design doc as *why the choice matters*, not as a rule the agent enforces.
- **The booking call identifies the patient.** Name, date of birth, age, phone and insurance provider + policy number all go to the clinic that ends up holding the appointment — a real clinic can't book an anonymous slot. The clinic *search* calls still say nothing about the patient, and `describe_goal()` discloses the whole thing in the consent text before anything is dialled. This is a deliberate divergence from the Accommodation Broker's stricter vault rule (Idea 1 in the design doc), which is unchanged for that idea.
- **Clinic search yield isn't guaranteed, though it's far better than it was.** The Maps scraper returns the listing itself, so name/category/phone/ZIP arrive structured — no markup scraping, no aggregator filtering. Places are still dropped for: no callable US number, no 5-digit ZIP, closed, non-US, a duplicate phone (multi-location practices share one central number), or a category that doesn't plausibly match what was searched for. 20 places are requested to fill a list of 10. With zero survivors — or no token, an auth error, a 402, a failed run — the lookup falls back to `data/clinics_fallback.json`, **synthetic, not a cached snapshot of real businesses**, tagged `source="fallback_snapshot"`.
- **The category filter is a keyword allow-list, not semantics.** Maps returns adjacent and sponsored businesses (a sunglasses shop for "optometrist"), and nothing downstream reads the clinic's category — the CALL-E task text is built from the *user's* requested appointment type — so an off-category listing would be phoned with the wrong script. `CATEGORY_KEYWORDS` in `clinic_lookup.py` is the guard; widen it if a legitimate clinic type gets filtered out.
- **"Nearest-first" is ZIP-granular, and discards Maps' own ordering.** Distance comes from ZIP centroids, so every clinic sharing the user's ZIP scores 0.0 and ties fall back to the order Maps returned. A ZIP outside `data/nv_zip_centroids.json` sorts *last*, so a clinic just over a county line can rank below a farther one across the valley. Carrying each place's coordinates would fix this; the record is deliberately four fields.
- **A clinic search takes tens of seconds and blocks.** `find_clinics()` starts an Apify actor run and polls it (3s interval, 240s deadline) inside the synchronous workflow.
- **The freelance leg is bounded by the roster, not by a shortlist.** A 15-mile radius around Las Vegas reaches ~20 seeded interpreters, so a run where nobody matches can spend that many calls. No cap was added — the batch search stopping at the first match is the only brake. One line if you want one.
- **The asymmetric-commit safety property has been traded away, knowingly.** The finalized sequence confirms an interpreter *before* the clinic is called back to book for real — the reverse of the ordering this project was originally built around. If that booking call then fails, a fee-bearing interpreter engagement exists with no appointment behind it. **Nothing releases them automatically** (`send_release()` is still unwired), so `_require_booked()`'s error names the interpreter and says the user must phone them directly before any cancellation window closes. This is the "Flagged tension" callout in the design doc, resolved as *accept the residual risk*.
- **`has_interpreter: true` collects nothing about that interpreter.** No name, no number, no availability — so clinic slots are matched against the user's own windows exactly as on the full path, one matching slot is picked at random, and nobody is called or texted on that interpreter's behalf. The user gets the only confirmation text, and the result carries `{"tier": "user_arranged"}` with no name.
- **Family and freelance matching now depend on exact slot-string equality** (`"2026-09-24 14:00"`). Family used to be matched structurally on `TimeWindow` objects; it is now asked which of the already-matched slots it can cover, and the answer is compared as a string. A real call answering `"14:00:00"`, `"2026-09-24T14:00"`, or `"Thu 2pm"` silently yields no match and reports the family list exhausted. New failure surface, distinct from the clinic date-parsing gap below.
- **Satisficing, not optimizing — by design.** "Nearest" and "cheapest" are only ever compared *within* the batch that first matched, so two runs against the same clinic/interpreter pool can book different people depending on batch order.
- **A freelance candidate who is available but gives no hourly rate is not counted as a batch match**, and the search continues to the next batch. This keeps an unpriced candidate from reaching the binding confirm call (which would read "$None/hr" to a real person), at the cost of a genuinely available responder not stopping the search.
- **Newly unreferenced, kept deliberately:** `interpreter_matching.rank_by_cost()` (the tie-break is by hourly rate, per the design doc's wording), `clinic_call.cancel_booking()` (its old caller was the all-decline path, which under the new ordering has no booking to cancel), and `TimeWindow.intersection()` (its only caller was the old arranged-interpreter constraint math).
- ~~`load_candidates_within_radius(clinic_zip, ...)` is un-wireable from the live flow.~~ **Fixed.** The clinic is discovered in Step 2 and `ClinicCandidate` now carries a ZIP, so the roster is loaded within travel distance of the *matched clinic* — which is what the design always specified.

**Pre-existing, still open:**

- ~~Family-by-phone-call captures nothing.~~ **Fixed 2026-09-13, then superseded the same day.** The fix (parsing a family member's free windows out of the call) was real, but the finalized workflow removed the question it answered: family is now called *after* a clinic match and asked which of the already-matched slots it can cover, so `free_windows` harvesting no longer exists. `family_tier_result` still collapses "declined/no-answer," "answered with no overlap," and "gate blocked" into one `"no_overlap"` value.
- ~~Freelance fan-out is one call per candidate, not CALL-E's native batch.~~ **No longer a gap.** The finalized design says outright that clinics, family, and freelance interpreters are "each rung individually in small groups (3 at a time), not via one native multi-recipient call" — one `call_and_wait()` per recipient is now the specified behaviour, not a shortfall against it.
- **`rank_by_cost()` drops a candidate with no stated minimum-hours** (`minimum_hours: null` reads as "can't compute cost," not "no minimum applies") — the doc's own worked example features exactly this kind of candidate.
- **RARE REVERSAL isn't wired up.** `interpreter_matching.send_release()` exists and works but nothing calls it — there's no code path today that notices a clinic booking got cancelled after an interpreter already confirmed.
- ~~Phase 4's family-notify call is missing.~~ **Addressed in part:** the secured interpreter — family or freelance — now gets the final confirmation text alongside the user, though delivery itself is still a stub (see the table above).
- **`AppointmentResult`'s shape drifts from the doc's** in a few fields: no booked date/time survives into the top-level result on any path, and `cancellation_deadline` is always `None`.
- **Clinic-returned date/time strings aren't validated or normalized** before `datetime.fromisoformat()` parses them — a transcript returning "Sept 24th" instead of "2026-09-24", or "2:30 PM" instead of "14:30", raises after the (real, paid) search call has already happened. Note this now sits on the demo path: the *user's* side is validated hard, but the clinics answering are real. Still out of scope, deliberately.
- **`TERMINAL_STATUSES`'s wider set** (`NO_ANSWER`, `VOICEMAIL`, `BUSY`, `DECLINED`, `EXPIRED`) **isn't supported by the SDK's own generated models**, which only define `canceled/completed/failed/in_progress/queued`. The code is harmless either way, but the comment overclaimed a "confirmed inconsistency" that the SDK's source doesn't actually back up — corrected to note this is unconfirmed beyond the (now-uninstalled) CLI docs.
- **The run still mutates caller-supplied objects** — `ClinicCandidate`, `FamilyInterpreter`, and `InterpreterCandidate` all have their answers written back in place, so re-running with the same objects starts from a dirty state. `UserInput` itself is never mutated, and the roster loader sidesteps the problem by caching raw dicts and building fresh candidates per call — but a caller who hand-builds a list and reuses it still gets stale answers the second time.

## Next steps, roughly in order

1. Decide which of the "Known limitations" above are worth fixing before the deadline vs. documenting as-is — the unreleased-interpreter case after a failed booking call is the one with real money attached; clinic time-string normalization is the one most likely to bite on camera.
2. Add `APIFY_API_TOKEN` and run one live clinic lookup against a real NV ZIP — with `allow_fallback=False`, so a bad token can't hand back ten synthetic clinics that look like success — to confirm the real field names and the actual yield before relying on it in a recording.
3. Pick an SMS/email/push provider for `reminders.send_confirmation_text()`, and decide whether the calendar write is worth adding.
4. When the ASL recognizer is ready: have it produce its share of the input JSON (appointment type, availability, has_interpreter) and merge it with the registered profile fields, instead of the harness's hand-built object.
5. Record the demo: the eleven scenarios map onto the "Result scope" beats in `CALL-E Hackathon Ideas.md` — `clinic_search` is the main run end to end, `shortcut` the second run, `input_validation` the "fails free, up front" beat. **Budget first:** the worst case is now 20–30 real calls against 20 free ones, since the freelance leg draws on the whole roster.
