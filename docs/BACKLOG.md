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

## Warm sessions need more real-world testing (Phase 10)

Feature is built and unit-tested. Open questions from live use:

- **Priming refusals.** A bare-declarative `priming_message` (e.g. "You are
  helping me plan a trip.") is often bounced by Gemini Web with "I'm having a
  hard time fulfilling your request." Use a real conversational opener with a
  question. Consider: detect a refusal in the priming turn and fail
  `POST /v1/sessions` with a clear error instead of returning a session backed by
  a broken exchange.
- **Continuation reliability.** Follow-up turns through a session have
  intermittently returned Gemini's generic "I encountered an error" — needs more
  runs to tell apart transient flakiness vs. a real continuation bug (was worse
  before the `for_session` / `model=None` fix; retest).
- **Latency benefit unbenchmarked** (SRS 2.11 asks for before/after numbers):
  same prompt as (a) a fresh `/v1/chat/completions` vs (b) a follow-up on a
  session, several times each. The account was too fragile during dev for
  repeatable timing.

Until this settles, treat warm sessions as experimental. The stateless endpoints
are the solid path; sessions are strictly opt-in and don't affect them.

## Self-heal vs. SRS §7 tension

`app/self_heal.py` re-inits the client when it's degraded, but SRS §7 says rapid
re-auth *causes* degradation. Mitigated with a 10-min base interval + exponential
backoff to 1h. If an account is degraded *because* of prior re-init storms, the
healer's own attempts could prolong it — watch `self_heal.attempts` climbing
without `recoveries` and consider raising `self_heal_interval` or setting it to 0
and recovering by hand.

## ~~Docker image (SRS 3)~~ — done in Phase 11

`Dockerfile` + `docker-compose.yml` + `docs/DEPLOYMENT.md`, `/data` volume for the
rotated-cookie cache / activity.db / admin_credential. Built and smoke-tested.

## ~~Admin dashboard UI~~ — reworked

Now a static client-rendered page in `app/static/` (`index.html` + `dashboard.css`
+ `dashboard.js`), polling `/admin/status.json`. Served at `/admin` and at `/`
(for `Accept: text/html`). To drop in a different front end, replace `app/static/`
— the JSON contract (`/admin/status.json`, `POST /admin/cookies`, the auth forms,
the `gop_admin` cookie) is stable.
