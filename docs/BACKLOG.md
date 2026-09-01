# Backlog

Deferred work — not blocking, revisit later.

## Tool-calling reliability (Phase 5)

**Status:** parsing + response shaping done and tested (77 tests). Injection
wording does **not** reliably make Gemini 3 (`gemini-flash`) emit the tool-call
syntax — even with `tool_choice: "required"` it answers inline.

Tried (all in `app/tools.py:build_tool_instructions`, verified live):
- instructions before the conversation → ignored
- instructions after the conversation, moderate wording → ignored
- forceful "this turn is a tool-call turn, do not answer" → still answered
- "you have no search / live data this turn, tools are the only route" → model
  *acknowledged* the constraint but still refused rather than emitting a call

Likely cause: Gemini Web has its own search grounding and won't role-play a
foreign tool protocol when it believes it can answer.

Ideas not yet tried:
- a persisted Gem (`gemini_webapi.create_gem`) as a strict function-calling persona
- response pre-fill / continuation (end the prompt mid-fence) — may not work via
  the web protocol
- only inject for tools with no knowledge-substitute; accept that
  `get_weather`-style tools will often be answered inline
- try `-high` (extended thinking) — the model may reason itself into compliance

Tool calling *does* work end-to-end when the model emits the syntax (covered by
tests with a canned reply); this is purely a compliance-rate problem.

## Warm-session latency benefit unverified (Phase 10)

SRS 2.11 says to measure the cold-vs-warm latency delta before *and* after
building the feature. The feature is built and correct, but the benchmark hasn't
been run (the Gemini account was too fragile during development to run repeatable
timing). Before relying on warm sessions for a latency win, measure: same prompt
as (a) a fresh `/v1/chat/completions` call vs (b) a follow-up turn on a
`POST /v1/sessions` session, several times each.

## Docker image (SRS 3)

A `Dockerfile` + `docker-compose.yml` with `data_dir` (cookie cache, activity.db,
admin_credential) mounted as a volume. Not started.

## Admin dashboard UI (Phase 8)

`app/admin.py` serves a minimal built-in HTML page. The operator has their own UI
to drop in later. The JSON contract is stable — `/admin/status.json`,
`POST /admin/cookies`, and the admin-auth forms — so a replacement front end only
needs to talk to those; `GET /admin` (the HTML) is the only thing that changes.
