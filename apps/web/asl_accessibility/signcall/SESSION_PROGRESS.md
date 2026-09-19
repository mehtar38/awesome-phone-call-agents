# signcall — session progress log (Interpreter Mesh build)

Covers everything from "let's build Idea 2" through the current state. Written as a context-preservation snapshot before a context-window compaction — treat this as the source of truth for what's been decided and done; re-derive nothing from memory that's written here.

## Where this sits

- **Design doc**: `/Users/kanishgodani/Claude Code Dir/CALL-E Hackathon Ideas.md` — "## 2. Interpreter Mesh" is the full spec this code implements. Only 2 ideas remain in that doc (Accommodation Broker, Interpreter Mesh) after earlier pruning; Interpreter Mesh is the committed build.
- **Code**: `/Users/kanishgodani/Claude Code Dir/apps/signcall/` — this project.
- **Hackathon**: CALL-E "Your Code Is Calling", deadline **Sept 14, 2026 @ 11:45pm SGT**.
- **Custom project subagents**: `.claude/agents/asl-debugger.md` (Sonnet, on-demand debugging) and `.claude/agents/plan-pressure-tester.md` (Opus, pre-ExitPlanMode adversarial review) — both created this session, both now working (confirmed loaded after an initial session where they weren't).
- **Project `CLAUDE.md`**: has a standing rule to spawn `plan-pressure-tester` before calling `ExitPlanMode`.

## Architecture (settled, don't re-litigate)

```
webcam clip → [ASL recognizer, built SEPARATELY, not in this repo] → gloss ─┐
                                     registered profile (identity/insurance) ─┴→ user input JSON
                                                                                    │
                                                            workflow/ + calle/ (pure text/JSON)
                                                                                    │
                                                                    CALL-E places real phone calls
                                                                                    │
                                                structured_result → text + gloss playback
```

- **CALL-E SDK only** (`calle-ai` on PyPI, v0.7.0, pinned in `requirements.txt`). MCP, the CLI, and raw REST were all deliberately **not used** — the CLI + its agent skill were installed once (per the official CALL-E install guide) then **fully uninstalled** (npm package removed, skill removed via `npx skills remove`, OAuth session logged out, `~/.calle-mcp` cache cleared) once it became clear the SDK was the right call for actual app code. Nothing in the codebase touches anything but `calle-ai`.
- **Consent gate is pure local logic, not a CALL-E feature.** `calle/plan.py::create_plan()` makes zero network calls. There's no SDK/REST "plan without dialing" endpoint — that two-step shape is an MCP-only abstraction. Nothing touches CALL-E until `workflow/appointment.py` has rendered the plan and gotten a user "yes".
- **Naming collision (real, hit during dev):** `calle-ai` installs a package literally named `calle`. This app's own adapter is *also* `calle/`. Fix: **always run from `apps/`, never from `apps/signcall/`** — `python3 -m signcall.frontend.text_harness ...` from the `apps` directory. Running from inside `apps/signcall/` puts that dir on `sys.path` and silently shadows the real SDK with the local adapter (confirmed reproduction, documented in README).
- **Python: Homebrew 3.12 only, system-wide, not Anaconda or Apple's bundled 3.9.** `calle-ai` requires Python ≥3.11. Fixed across **three** shell config files (not just `.zshrc`) because zsh sources different files for different shell classes and macOS's own `path_helper` reorders PATH in between them:
  - `~/.zshenv` (created, didn't exist before) — covers non-interactive/non-login shells (scripts, CI).
  - `~/.zprofile` — covers login shells, which run *after* `path_helper` demotes PATH.
  - `~/.zshrc` — covers interactive shells, which run after conda's own init hook re-prepends its base env.
  - Also removed a dangling, harmless-but-real python.org 3.14 PATH entry from `.zprofile`.
  - Verified across all four shell classes (login/non-login × interactive/non-interactive) with clean-environment (`env -i`) tests — all resolve to Homebrew 3.12.14.
  - **When resuming work in a NEW terminal, `python3 --version` should just say 3.12.x.** If a tool/shell shows 3.9.x, it's using a stale/cached environment predating these fixes (this happened mid-session with the Bash tool's own snapshot mechanism — override with `export PATH="/opt/homebrew/opt/python@3.12/libexec/bin:/opt/homebrew/bin:$PATH"` if that happens again).
- **Credentials**: `apps/signcall/.env` holds `CALLE_API_KEY=...`, loaded automatically by `calle/__init__.py` via `python-dotenv` (added to `requirements.txt`). `.env` is gitignored; `.env.example` is the committed placeholder. **An earlier API key was pasted into chat by accident and was revoked** — the current key in `.env` is a fresh one, never typed into a command or into this conversation. Never ask the user to paste a key; if a command needs one, tell them to edit `.env` directly or run the command themselves via `!`.

## Repo layout

```
apps/signcall/
├── __init__.py                  ← makes `signcall` a real package
├── .env / .env.example / .gitignore / requirements.txt
├── asl/                         ← NOT scaffolded — recognizer being built separately;
│                                   it feeds the signable part of schemas/user_input.schema.json
│                                   (appointment_type, availability, has_interpreter)
├── schemas/user_input.schema.json ← THE input contract, validated on every run
├── data/                        ← interpreters_nv.json (30 synthetic), nv_zip_centroids.json
│                                   (GeoNames CC BY 4.0), clinics_fallback.json (synthetic)
├── api/                             NEW — server.py (POST /runs, GET /runs/{id}, GET /health),
│                                     demo_mocks.py (opt-in per-run scripted answers)
├── workflow/                        (as of the 2026-09-13 input/lookup/roster rewrite)
│   ├── types.py                     UserInput (replaces Intent), Insurance, ClinicCandidate
│                                     (zip/type/source), FamilyInterpreter (relation),
│                                     InterpreterCandidate, ClinicSlot.key(), distance_sort_key
│   ├── user_input.py                NEW — load_user_input(): jsonschema validation, "2PM" →
│                                     14:00-15:00, weekday/date + past-date + NV-ZIP checks
│   ├── calendar.py                  matched_slots() — clinic slots ∩ user availability
│   ├── clinic_lookup.py             find_clinics(): Apify Google Maps Scraper by ZIP + type,
│                                     four-field records (schemas/clinic_record.schema.json),
│                                     drop/dedupe filters, haversine ordering, dump file,
│                                     synthetic fallback
│   ├── clinic_call.py               search_clinics (batches of 3, insurance-gated),
│                                     ask_insurance_then_slots, book_slot (now carries patient
│                                     identity + insurance), _require_booked, cancel_booking
│   ├── family_call.py               NEW — call_family_in_order(): ordered, call-only,
│                                     one at a time, stops at the first yes
│   ├── interpreter_matching.py      search_freelancers_in_batches, rank_by_rate,
│                                     slot_for_candidate, confirm_interpreter,
│                                     rank_by_cost (kept, now unused), send_release (unwired)
│   ├── reminders.py                 non-CALL-E notifications: confirmation_text_body +
│                                     send_confirmation_text (stub, caught by appointment.py)
│   └── appointment.py               orchestrator: Step 2 search → Step 3 (shortcut |
│                                     family | freelance) → Step 4 book + notify
├── calle/
│   ├── __init__.py                  loads .env, sets MOCK_MODE
│   ├── plan.py                      local-only consent gate
│   ├── run.py                       call_and_wait() — the ONLY function touching CALL-E;
│                                     DEMO_TEST_LINES + resolve_dial_target() (real mode dials
│                                     ONLY the 3 owned test lines); _map_result,
│                                     _extract_confidence, _extract_transcript_turns
│   └── result.py                    was_successful/evidence_summary (currently dead code —
│                                     nothing calls them yet)
├── frontend/
│   ├── text_harness.py              10 asserting scenarios, all offline (see below)
│   └── live_smoke_test.py           standalone REAL-call diagnostic, not part of the
│                                     workflow; takes a phone number OR an existing call_id
│                                     to re-inspect without spending another call
├── .venv/                           Homebrew Python 3.12, calle-ai==0.7.0 + python-dotenv
├── requirements.txt
└── README.md                        comprehensive — architecture, setup, known limitations
```

## Real-call history (only 2 real calls placed so far, to the user's own number +18722794605, with explicit consent)

1. **First real call** (`call_BUmjU8-F3q3Xd7mUeBGylA`) via `live_smoke_test.py`: connected, held a real conversation, completed. Crashed on `_map_result()` because `completion_confidence` came back as `{"score": 0.9, "label": "high"}`, not the plain float the docs implied. Re-inspected the SAME call (no new call spent) after fixing — also found `transcript_turns` was silently empty because the real field lives nested at `recipients[0].attempts[-1].transcript_turns`, not top-level. **Both fixed** in `calle/run.py`.

## Two-agent audit (plan-pressure-tester + asl-debugger, spawned in parallel) — the big finding

After the two mapping fixes, the user asked for both custom subagents to check for anything else wrong before spending another real call. Combined result: **the project's central safety claim (never ask an interpreter to commit before the clinic slot is real) was not actually enforced**, plus one crash on the default happy path. All now fixed:

### Fixed
1. **`coverable_slots` was thrown away** (confirmed with an actual executed test, not just code reading): ranking picked cheapest-by-cost with no check on which slot a candidate actually said she could cover — an interpreter who could only do Monday got booked/confirmed for Thursday. **Fix**: `InterpreterCandidate` now stores `coverable_slots: list[str]`; new `interpreter_matching.select_slot_and_candidates()` walks viable slots in order and only ranks candidates who explicitly listed that slot. `appointment.py` now gets `(target_slot, ranked)` together instead of picking a slot and ranking separately.
2. **`clinic_call.book_slot()` never checked success** — a failed/voicemail'd clinic call was silently treated as booked, and the code would proceed to ask an interpreter for a real commitment against a booking that might not exist. **Fix**: new `_require_booked()` raises immediately if `task_completed` is false or `structured_result["booked"]` isn't true, *before* any interpreter is contacted. Applied to both `book_slot()` and `merged_discover_and_book()`.
3. **Clinic could get booked even with zero viable interpreter candidates** (2 wasted real calls: book then immediately cancel). **Fix**: `appointment.py` now checks `target_slot is None` and raises before ever calling `book_slot()`.
4. **SHORTCUT path discarded the interpreter's availability constraint entirely** — `constraint_windows` was computed then never used in the task text sent to CALL-E, so the clinic could book any slot including ones the arranged interpreter couldn't attend. Also, the constraint computation itself used `.overlaps()` (keeps the whole window) instead of the already-existing-but-never-called `TimeWindow.intersection()` (the actual overlap). **Both fixed** in `appointment.py::_run_shortcut()` and `clinic_call.py::merged_discover_and_book()` (now describes the actual windows in the task text).
5. **`_send_share_link()` crashes unconditionally, and it's the DEFAULT family channel** for any non-urgent family member who doesn't prefer calls — this killed the entire run on the happy path. **Fix**: `appointment.py` now catches `NotImplementedError` there and degrades to "family unavailable this run" instead of crashing.
6. **Two confirmed crash cases** from edge-case testing (malformed `recipients`/`attempts` shapes in a raw API response) — added `isinstance` type guards in `_extract_transcript_turns()`.
7. **New permanent regression test** added: `cheapest_wrong_day` scenario in `text_harness.py`, reproduces the exact bug from #1 and asserts the fix holds. All 4 scenarios (`routine_freelance`, `shortcut`, `decline_then_retry`, `cheapest_wrong_day`) pass.
8. Cleaned up several stale/contradictory docstrings and README claims (a same-file date typo, a claim that live-response checking "hasn't happened yet" sitting right above code that says it has, an overclaimed "confirmed inconsistency" about `TERMINAL_STATUSES` that the SDK's own generated OpenAPI models don't actually support).

### Documented as known limitations, NOT fixed (deadline tradeoff — see README's "Known limitations" section for full detail)
- **Family-by-phone-call captures no availability data** — `reminders._call_family()` places the call but never parses a response into `free_windows`. Family match is currently unreachable whenever reached by call (which is the *default* for urgent/emergency cases) — this is the most important remaining gap, since family is supposed to be the free, first-checked tier.
- Freelance fan-out is one `call_and_wait()` per candidate in a loop, not CALL-E's native batch `recipients: [...]` parameter — functionally safe (confirmed by audit) but doesn't match the design doc's "one batch call" framing.
- `rank_by_cost()` drops any candidate with `minimum_hours: null` (no stated minimum) instead of treating that as "no minimum applies" — the doc's own worked example features exactly this kind of candidate.
- `RARE REVERSAL` (`interpreter_matching.send_release()`) is defined and works but nothing calls it — no code path notices a clinic cancellation after an interpreter already confirmed.
- Phase 4's "call family to tell them the confirmed time" step is missing after `_book_with_family()`.
- `AppointmentResult` shape drifts from the doc in a few fields: no booked date/time survives to the top-level result on any path, `cancellation_deadline` is always `None`, `family_tier_result` doesn't distinguish 3 real distinct outcomes (gate-blocked vs. no-overlap vs. never-registered).
- Clinic-returned date/time strings aren't validated/normalized before `datetime.fromisoformat()` parses them — a transcript saying "Sept 24th" instead of "2026-09-24" would raise, after the (real, paid) discovery call already happened.
- `evidence`'s and `task_completed`'s actual *types* are confirmed correct from 2 real calls but not independently guaranteed by anything else — still using defensive `.get()` fallbacks.
- `run_interpreter_mesh()` mutates the caller's `Intent` object (`intent.family = None`) — re-running the same `Intent` twice gives different results.
- Interpreter roster/directory lookup (`load_candidates_within_radius`) is still a stub — pass a hand-built candidate list.
- ASL recognizer not started here at all (separate track).

## Bottom line as of last exchange

User asked "is the workflow ready?" Answer given: **core mechanism yes (asymmetric commit now genuinely enforced, proven against 2 real calls, 4/4 test scenarios passing), full spec no** — family-by-call gap is the one flagged as worth a decision (fix it, or route demos around it via the SHORTCUT path or pre-populated family availability).

## If resuming after context compaction

1. Re-read this file first.
2. Check `git status` / `git log` in the repo if unsure what's committed vs. still working-tree only (nothing has been explicitly committed to git during this build as far as this log tracked — verify before assuming).
3. To re-verify nothing regressed: `cd apps && source signcall/.venv/bin/activate && for s in clinic_search shortcut decline_then_retry cheapest_wrong_day family_locks_in clinic_search_exhausted input_validation clinic_lookup roster_load call_routing; do python3 -m signcall.frontend.text_harness $s; done` (all 10 should pass; they assert in-process, so a silent pass really is a pass).
4. Remaining hackathon work not yet started: shaping repo docs to the `awesome-phone-call-agents` contribution template, `validate_repository.py`, opening the PR, recording the ~3min demo video, Devpost submission + feedback survey.

## Update (2026-09-13, post-compaction session)

The family-by-call gap flagged above (line 95, "the most important remaining gap") is **fixed**. `reminders._call_family()` now actually parses the call's structured response into `family.free_windows` instead of discarding it, with tz-naive normalization and per-entry malformed-input guards (`_parse_free_windows`). A `plan-pressure-tester` review of the first draft caught two more real bugs before implementation: no date anchor in the call task (family would answer relative to nothing) and no timezone handling (an aware timestamp would crash `calendar.slot_in_windows` with an uncaught `TypeError`) — both fixed in the shipped version. It also caught that the appropriateness gate (`sensitivity == ROUTINE`) wasn't checked before placing the family call, so a complex/sensitive + urgent visit would still spend a real call on family and then discard the answer regardless — fixed by hoisting the gate check into Phase 0.

Three new mock scenarios added to `text_harness.py` (`family_by_call`, `family_by_call_urgent`, `family_call_gate_blocked`) — all passing, along with the original four. Full detail in `README.md`'s "Known limitations" section (the bullet is struck through, not deleted, so the history stays visible) and the plan file this was built from: `/Users/kanishgodani/.claude/plans/in-the-first-4-resilient-dijkstra.md`.

Still open, not touched by this fix: `family_tier_result` still collapses "family declined/no-answer," "family answered with no overlap," and "gate blocked" into one `"no_overlap"` value — distinguishing them would mean threading a result code from Phase 0 through to `_book_then_confirm`, deferred as a larger change. Text/share-link delivery for family (`_send_share_link`) also remains an unimplemented stub — this fix was scoped to the call path only, per explicit instruction.

## Update (2026-09-13, later same session) — workflow logic finalized, NOT yet implemented

The user dictated, refined over several rounds, and explicitly finalized ("do not alter anything at all") a new call sequence for the whole Interpreter Mesh workflow. **`CALL-E Hackathon Ideas.md`'s "## 2. Interpreter Mesh" section is now the authoritative spec for this** — its Scope, Use case, and Workflow subsections were rewritten to match verbatim. Read that file, not this summary, for the actual sequence. High-level shape:

- User profile parsed into structured JSON (as before — this is `Intent`).
- **Clinic is now searched for, not given.** Up to 10 nearby clinics, nearest-first, batch-called 3 at a time; each is asked whether it accepts insurance *before* being asked for slots (a new gate — clinic policy, not the patient's own coverage, which is still never voiced to the clinic). Stops at the first batch of 3 with a matching clinic; nearest among that batch's matches wins.
- **Family is now call-only, a definite list, called AFTER the clinic match** (reordered from today's before-clinic ordering) — one at a time, in list order, against the already-matched slots; stops at the first member who says yes. No share link, no urgency-based channel selection, no wait window.
- **Freelance interpreters are searched in batches of 3** against the matched slots, each asked for their rate; stops at the first batch with a match, cheapest within that batch wins — **not** a global-cost rank across every candidate like today's design.
- Ends with: clinic called back to book for real, added to the user's calendar, **and a final confirmation text sent to both the user and the interpreter** (family or freelance) — this last step is new and not in the current code at all.

**None of this is implemented yet.** The current code in `workflow/` and `calle/` still reflects the OLDER design this replaces: single `clinic_phone` given up front (not searched), family as a single optional member reached via share-link-or-call, global-cost ranking via a simulated batch fan-out, no insurance gate, no final SMS step. Two earlier candidate plans toward pieces of this (a `pgeocode`-based interpreter-roster lookup, and an intermediate family-call-only/multi-member design) were drafted and adversarially pressure-tested in this session but were **explicitly discarded, unimplemented, at the user's request** before this final workflow was dictated — do not resurrect either from memory; this update supersedes both.

**One tension flagged in the design doc itself, not resolved here:** the new sequence confirms the interpreter (family or freelance) *before* calling the clinic back to book for real — the reverse of this project's original asymmetric-commit safety property (book the free/cancelable clinic leg first, only ask the fee-bearing interpreter to commit once that's secured). Under the new order, a real interpreter engagement can exist with no clinic booking behind it if the callback booking then fails. This is called out explicitly in `CALL-E Hackathon Ideas.md` right after the Use-case JSON block, with options for whoever implements this to consider (a last-second re-confirm with the clinic, reordering the ask, or accepting the residual risk) — it was not silently decided either way.

**If resuming to implement this:** treat `CALL-E Hackathon Ideas.md`'s Idea 2 section as ground truth, not this file or any older code comment. Expect this to touch `types.py` (Intent needs a clinic list/search concept, not a fixed `clinic_phone`; family becomes a list; an interpreter-rate field on the profile), `calendar.py`/`clinic_call.py` (new insurance-gate + batched multi-clinic search), `interpreter_matching.py`/`family_call.py` (batch-of-3 semantics replacing today's "call everyone" fan-out and single-member family check), and `appointment.py` (the whole phase ordering). Budget-check any implementation against the design doc's revised 15–20+ call worst case before assuming the existing mock-scenario call counts still apply.

## Update (2026-09-13, later still) — finalized workflow IMPLEMENTED

The sequence dictated in the section above is now in the code. `CALL-E Hackathon Ideas.md`'s Idea 2 section remains ground truth; this records what landed against it.

**What changed**

- **Clinic is searched, not given.** `Intent.clinic_phone` is gone. `clinic_call.search_clinics()` sorts a caller-supplied `ClinicCandidate` list nearest-first, caps it at 10, and calls them in batches of 3. Each clinic gets ONE call whose task asks the insurance-acceptance *policy* question first and only asks about slots if the answer is yes (the agent's own conversational branch — which is why 10 clinics costs ~10 calls, not 20). The first batch containing at least one insurance-accepting clinic with a slot in the user's windows ends the search; the nearest match in that batch wins.
- **Family is call-only, an ordered list, checked AFTER the clinic match.** New `workflow/family_call.py`. Members are called one at a time, in list order, and asked which of the already-matched slots they can cover; the first yes is locked in for the slot they named and nobody further is called. The appropriateness gate still blocks the whole tier for complex/sensitive visits before any call is placed. Share-link delivery, `prefers_call_over_link`, urgency-based channel selection, and free-window harvesting are all gone.
- **Freelance is batches of 3, cheapest-within-batch by hourly rate.** `search_freelancers_in_batches()` replaces the global-cost fan-out. `confirm_interpreter()` remains the single binding ask, and a decline falls through to the next match *in that same batch* (the search has already stopped). The confirmed candidate's slot is derived from their own answer, so the `cheapest_wrong_day` invariant holds structurally now rather than via a separate slot-walk.
- **Step 4 sends two confirmation texts** (user + whichever interpreter was secured) via `reminders.send_confirmation_text()`. Delivery is a stub — no SMS provider is named by the design — so `appointment.py` catches the `NotImplementedError` and records the exact undelivered message in `evidence`.

**Three decisions the user made during planning**

1. **Commit ordering: accept the residual risk.** Interpreter confirmed before the clinic books, verbatim per the doc. No re-confirm call, no reordering. `_require_booked()` now names the confirmed interpreter and says outright that they are not released automatically and must be phoned directly.
2. **SHORTCUT reuses the same clinic-search subroutine** (insurance gate + nearest tie-break), matching slots against the **user's availability only** — the arranged interpreter is assumed to share it, so `ArrangedInterpreter.free_windows` is no longer intersected in — and picks **randomly** among multiple matched slots. This follows the doc's Workflow block over one contradicting sentence in its Scope prose; the deviation is written into README rather than left to be discovered.
3. **Freelance tie-break sorts by hourly rate** (the doc's wording). `rank_by_cost()` is untouched and now unused.

**Explicitly NOT done this pass** (still open, unchanged): real clinic/interpreter directory lookups, `rank_by_cost()`'s null-minimum-hours drop, RARE REVERSAL (`send_release()` still unwired — including for the new failed-booking-after-confirm case), `family_tier_result`'s three collapsed outcomes, `AppointmentResult` shape drift, clinic date-string normalization, and the calendar write in Step 4 (no mechanism specified, none invented).

**Tests**: `frontend/text_harness.py` rewritten to seven scenarios — `clinic_search`, `shortcut`, `decline_then_retry`, `cheapest_wrong_day`, `family_locks_in`, `family_gate_blocked`, `clinic_search_exhausted` — all passing in mock mode, all asserting in-process. Mock resolvers now branch on the task text as well as the phone number, since the winning clinic is called twice per run (search, then book) and an interpreter up to twice. `family_by_call_urgent` was deleted (urgency-based channel selection no longer exists). **No real CALL-E calls were placed in this pass**; the real-call history above still stands at 2.

Plan file this was built from: `/Users/kanishgodani/.claude/plans/i-m-continuing-work-on-misty-cocoa.md` (pressure-tested before approval — that review caught a dataclass mutable-default crash, a `None`-rate sort crash, two unassertable test designs, and five docstrings that would have become false claims).

## Update (2026-09-13, final session) — input contract, clinic search, interpreter roster, call routing

Four changes, directed at both the codebase and `CALL-E Hackathon Ideas.md` (Idea 2). All ten harness scenarios pass; no real CALL-E call was placed.

**1. User input is now a JSON contract, and the appropriateness gate is gone.**
`schemas/user_input.schema.json` is the authoritative definition; `workflow/user_input.py` validates against that file with `jsonschema` (pinned) and parses it into `UserInput`. Fields: name, date_of_birth, age, phone_number, zipcode, appointment_type (physical_therapy | eyes | ent | dental | mental | general), insurance {provider_name, policy_number}, availability [{day, date, time}], has_interpreter, family_members [{name, relation, phone_number}]. Availability is **fixed timestamps** — `"2PM"` means free 2:00–3:00PM — and the clinic-search horizon is derived from the furthest date rather than supplied. Rejected before any call: weekday/date mismatch, a past date, family listed alongside `has_interpreter: true`, a malformed time, a non-NV ZIP, an unknown appointment type. **Deleted:** `Intent`, `Urgency`, `VisitSensitivity`, `ArrangedInterpreter`, `raw_text`, `need_type`, `window_days`-as-input, and every gate check (`gate_allows_family`, the shortcut's advisory print, the `family_gate_blocked` scenario).

Two consequences worth knowing: `has_interpreter: true` collects **no** contact details, so that path notifies only the user and returns `{"tier": "user_arranged"}`; and the **booking call now identifies the patient** (name, DOB, age, phone, insurance provider + policy number), while the clinic *search* calls still say nothing about them. `describe_goal()` — the consent text — discloses both the search breadth and that disclosure.

**2. Family: already call-only, now with `relation`.** Nothing text-based remained to remove (the share-link path went in the previous pass). A secured family member is still *texted* the confirmation once the appointment is booked, alongside the user — call-only governs how they're **secured**, not whether they're told the outcome.

**3. Clinics are searched, not supplied.** `workflow/clinic_lookup.py` finds them from the user's ZIP + appointment type. *(Superseded 2026-09-13 by the Apify swap — see the section below. As originally built this used the Google Custom Search JSON API, scraping name/phone/ZIP out of `pagemap` markup and snippets and filtering directory aggregators by hostname.)* Ordering is nearest-first by ZIP centroid (`data/nv_zip_centroids.json`, GeoNames CC BY 4.0), capped at 10. With zero usable results it falls back to `data/clinics_fallback.json`, which is **synthetic on purpose** — a committed file of real names paired with fetched numbers could misattribute a real practice's phone number.

**4. Interpreters come from a seeded synthetic roster.** `data/interpreters_nv.json`: 30 invented interpreters (name, age, phone, ZIP, city, expertise), 15 in Las Vegas and 15 across the rest of Nevada, every number in the reserved 555-01xx fictional block. `interpreter_matching.load_candidates_within_radius()` — the long-standing stub — is now real: it caches raw dicts (never candidate objects, which get mutated in place) and builds fresh `InterpreterCandidate`s filtered to 15 miles of the **matched clinic's** ZIP. Expertise is stored and surfaced, never used to filter. Rates stay off the roster; they're asked on the call.

**5. Real-mode call routing (new safety property).** `calle/run.py` defines `DEMO_TEST_LINES = ("+18722794605", "+17168683628", "+19496780146")` and a pure `resolve_dial_target()`. In real mode the dialled number is **always** one of those three, assigned by the recipient's position within the batch and sticky per recipient (searched on line 2 → booked on line 2). An unassigned recipient with no position **raises** rather than defaulting. `reset_call_routing()` runs at the top of each run so assignments don't leak between runs. `frontend/live_smoke_test.py` bypasses `call_and_wait()` by design, so it enforces the same list itself — otherwise the app would have had exactly one unguarded door.

**Explicitly NOT done** (unchanged): `rank_by_cost()`'s null-minimum-hours drop (now unreferenced), RARE REVERSAL / `send_release()`, `family_tier_result`'s collapsed outcomes, `AppointmentResult` shape drift, clinic date/time-string normalization (note this moved onto the demo path now that clinics are real), the Step 4 calendar write (no mechanism specified), and any cap on the freelance leg — a 15-mile Vegas radius reaches ~20 roster interpreters, so the design doc's budget warning was revised to 20–30 calls worst case.

**Scenarios** (all offline, all asserting): `clinic_search`, `shortcut`, `decline_then_retry`, `cheapest_wrong_day`, `family_locks_in`, `clinic_search_exhausted`, `input_validation`, `clinic_lookup`, `roster_load`, `call_routing`. `family_gate_blocked` and `family_by_call_urgent` are gone with the logic they tested.

Plan file: `/Users/kanishgodani/.claude/plans/i-m-continuing-work-on-misty-cocoa.md` (pressure-tested before approval — that review caught the `live_smoke_test.py` bypass, a booking call that collected patient data and never spoke it, a `None`-distance sort crash, the `MOCK_MODE`-bound-at-import problem in the routing test, and roster-cache contamination).

## Update (2026-09-13, later) — clinic lookup moved to Apify's Google Maps Scraper

Google Custom Search was the weak leg of the build: it returns web *pages*, so every field had to be guessed out of `pagemap` markup or snippet text, and directory aggregators had to be skipped by hostname to stop one business's name being paired with another's phone number. Apify's Google Maps Scraper returns the listing itself, so the guesswork is gone.

- **Contract:** new `schemas/clinic_record.schema.json` — a clinic is exactly `name`, `type`, `phone`, `zipcode`. Every record is validated against it; one that fails is dropped, never raised.
- **Transport:** `POST /v2/acts/compass~crawler-google-places/runs` → poll `GET /v2/actor-runs/{id}` (3s interval, 240s deadline; hyphenated statuses, only `SUCCEEDED` proceeds) → `GET /v2/datasets/{id}/items?clean=true`, which returns a bare array while the run endpoints wrap in `{"data": ...}`. Auth is an `Authorization: Bearer` header, not `?token=`, so the token can't land in a log line. `locationQuery` is `"<zip>, NV, United States"` — the actor errors the run when it can't geocode, and a bare ZIP is a known weak case.
- **Filters:** a place is dropped for no callable US number, no 5-digit ZIP, `permanentlyClosed`/`temporarilyClosed`, a non-US `countryCode`, a duplicate phone (multi-location practices share one central number, and `resolve_dial_target()` is sticky per phone), or a `categoryName` that doesn't match a per-type keyword allow-list. That last one matters because nothing downstream reads the clinic's category — the CALL-E task is built from the *user's* appointment type — so an off-category listing would be phoned with the wrong script. 20 places requested to fill 10.
- **`normalize_phone()` fixed:** it did an unanchored substring match, so `"+44 20 7946 0958"` produced `"+12079460958"` — a live US number belonging to an unrelated party. Now anchored to a full US match; anything else returns None.
- **Dump file:** every search writes `data/clinics_last_search.json` (wrapper + four-field records), atomically and inside a try/except so an inspection file can never break a lookup. Gitignored — it holds real business data.
- **Renames/removals:** `ClinicCandidate.appointment_type` → `clinic_type`, `result_url` gone (its only reader was `appointment.py`'s booking evidence line), `source` default now `"apify_google_maps"`. `GOOGLE_CSE_*` replaced by `APIFY_API_TOKEN` in `.env.example`. Deleted: `parse_result_item`, `extract_zip`'s regex fallback path, `clean_title`, `_pagemap_values`, `AGGREGATOR_HOSTS`, `QUERY_PHRASES`, `_query_cse`, `ClinicLookupError`.
- **Failure behaviour:** every failure (no token, auth, 402, 429, FAILED/ABORTED/TIMED-OUT, poll deadline, unresolvable location, zero survivors) degrades to the synthetic fallback. Nothing escapes `find_clinics()` — `appointment.py` calls it unguarded.

**Note on the roster:** `data/interpreters_nv.json` was edited outside the session so all 30 interpreters now carry the operator's three demo test lines instead of 555-01xx numbers. The `roster_load` scenario's number check was relaxed accordingly — the invariant it enforces is "no roster number can ring a stranger", which owned test lines satisfy. One consequence worth knowing: `resolve_dial_target()` is sticky *per logical phone*, so with only three distinct numbers across the roster, a freelance batch of 3 no longer spreads across three lines — all three candidates share whichever line their number was first assigned.

All ten scenarios pass; `clinic_lookup` now runs against canned Apify dataset items. **The live path is still unproven** — it needs `APIFY_API_TOKEN` in `.env` and one run with `allow_fallback=False`.

## Update (2026-09-13, final) — HTTP endpoint for the frontend

The frontend is built and lives outside this repo. It needed a way in; the only entry point was the text harness. Added `apps/signcall/api/`:

- **`POST /runs`** takes the user-input JSON exactly as `schemas/user_input.schema.json` defines it (no envelope, no client id — single user, no auth). Validation is synchronous, so a malformed profile returns **400 with the validator's own message and places no calls**. A valid one returns **202** with a `run_id` and the plan text, and runs on a background thread; the frontend polls **`GET /runs/{id}`** for `succeeded`/`failed`. **`GET /health`** reports mode and credential presence (never values). FastAPI + uvicorn, both pinned.
- **Submission is the consent** — no second confirm step. The response returns `create_plan(...).goal_text` rather than `render_plan_for_user()`, because that wrapper ends in "Confirm to proceed?" and the run has already started; asking would be theatre. `appointment._describe_goal()` became public `describe_goal()` so the API renders the same text the workflow does instead of a copy that drifts.
- **The server refuses to boot in real-call mode by accident.** `CALLE_MOCK_MODE` defaults to `0` and `.env` holds a working `CALLE_API_KEY`, so a bare `uvicorn signcall.api.server:app` would have come up ready to dial — and with submission-as-consent, one POST from an open tab would have spent ~15 real calls. Now it requires either `CALLE_MOCK_MODE=1` or an explicit `SIGNCALL_API_ALLOW_REAL_CALLS=1`. This was the most serious thing the plan review caught.
- **One run at a time**, `threading.Lock` released in a `finally` so a raising run can't wedge the server at 409; the worker is non-daemon so Ctrl-C can't kill the process inside the window where an interpreter has confirmed but the clinic isn't booked. Concurrency isn't cosmetic here: `reset_call_routing()` runs at the start of every run, so two overlapping runs would erase each other's line assignments and the booking call would raise `CallRoutingError` after a real commitment.
- **Demo mocks** (`api/demo_mocks.py`, `SIGNCALL_API_DEMO_MOCKS=1`) plus a `register_default_mock()`/`clear_default_mock()` hook in `calle/run.py`, consulted only when no per-number mock matches, registered per run and cleared in the same `finally`. Never registered at import, so the harness scenarios that leave numbers unmocked as tripwires keep failing loudly. The resolver is built from the submitted profile — a static one couldn't match an arbitrary user's availability windows, and must emit `HH:MM` since `"2PM"` makes `fromisoformat` raise.
- **CORS** limited to `http://localhost:*` / `http://127.0.0.1:*` via `allow_origin_regex`, documented as not being a security boundary.

**`.env` loading moved to `signcall/__init__.py`.** It lived only in `calle/__init__.py`, so importing `workflow/` without `calle/` (calling `find_clinics()` directly) left `APIFY_API_TOKEN` unset and silently degraded a live clinic search to the synthetic fallback — found while verifying the live path.

**The live Apify lookup is now proven.** `find_clinics("89101", AppointmentType.DENTAL, allow_fallback=False)` returned 10 real Las Vegas dental clinics with correct names, categories, phones and ZIPs, and the dump file (now written *after* sorting and capping, so it matches what the workflow actually called) holds exactly those 10.

**Clinic demo routing** needed no work — it was already enforced by `resolve_dial_target()`; re-verified.

Twelve scenarios pass (`api_endpoint` is new, using `fastapi.testclient` in-process with `APIFY_API_TOKEN` popped so it stays offline; its 409 assertion is made deterministic by a `threading.Event` gate the demo mock waits on). The server was also driven over real HTTP end to end in mock mode: 400 on a bad profile, 202 → poll → `succeeded` with a booked appointment, 409 while busy, 404 for an unknown id.
