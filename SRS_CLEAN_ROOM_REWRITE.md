# Software Requirements Specification: Gemini Web → OpenAI-Compatible API Gateway

## Purpose of this document

This is a clean-room specification for a **from-scratch implementation**. It describes
required behavior, API contracts, and known operational pitfalls learned from building
and running a working version of this system — it does **not** contain, reference, or
paraphrase any implementation source code. An implementer working from this document
alone should never need to look at (and must not look at) any prior codebase's source
files, including any other project with a similar goal.

**Hard constraint for whoever implements this**: no source code from any existing
"Gemini Web to OpenAI API" project may be copied, adapted, transcribed, or referenced
during implementation. This document specifies *what* the system must do and *why*
certain design decisions matter; *how* to write the code is entirely up to the
implementer, and independent implementations of the same requirement are expected to
differ in structure, naming, and approach. Where this document mentions a specific
third-party library (`gemini_webapi`), using that library as an installed dependency is
required and is not a copying concern — it is a public, independently-authored,
separately-licensed package, used the normal way any Python package is used.

---

## 1. System overview

### 1.1 What this system is

An HTTP service that authenticates to `gemini.google.com` (the consumer Gemini web
application) using browser session cookies, and exposes that authenticated session as a
programmable API, in two wire formats:

1. **OpenAI-compatible**: mimics enough of OpenAI's Chat Completions and Responses API
   surface that existing OpenAI-client tooling can point at this service instead, with
   no client-side changes beyond a base URL and API key.
2. **Google-native**: mimics enough of Google's own Generative Language API
   (`generativelanguage.googleapis.com`) request/response shape that tools built against
   the real Gemini API can point at this service instead.

### 1.2 What this system explicitly is not

- Not a wrapper around Google's official, billed Generative Language API. No Google
  Cloud project, no API key issued by Google, no billing account.
- Not affiliated with, endorsed by, or supported by Google.
- Not a multi-tenant commercial gateway. It is designed around one (or optionally a few)
  personal Google account's own Gemini access.

### 1.3 Core technical approach (required)

- **Language/framework**: Python, FastAPI, ASGI (uvicorn or equivalent).
- **Gemini connectivity**: via the `gemini_webapi` PyPI package
  (`https://github.com/HanaokaYuzu/Gemini-API`), pinned to a specific minor version range
  rather than left fully open-ended. This library is responsible for: cookie/session
  management, request signing, response stream parsing, live per-account model
  discovery, and cookie auto-refresh. **Do not hand-roll Gemini's internal wire protocol**
  — this was tried in an earlier iteration of a similar system and was the single
  largest source of reliability problems (see §7, Known Pitfalls).
- **Session model**: one shared, lazily-initialized client object per process, backing
  all requests. Concurrent requests share this one underlying connection (see §6.4 for
  why this needs an explicit concurrency bound).

---

## 2. Functional requirements by subsystem

### 2.1 Configuration

- A JSON config file, loaded from (in order of preference): an explicit path passed at
  startup, an environment variable naming a path, the current directory, then a
  user-level config directory (respecting the XDG base directory spec on Linux:
  `$XDG_CONFIG_HOME` or `~/.config` as fallback).
- A hardcoded set of defaults must exist so the service can start with zero
  configuration present.
- Config keys required (names are illustrative, not prescriptive):
  - Network: listen host, listen port, request timeout.
  - Default model identifier used when a request doesn't specify one.
  - Cookie source: a file path containing exported session cookies (see §2.2).
  - `api_keys`: list of strings; when non-empty, generation endpoints require one of
    these; when empty, generation endpoints are open (log a warning at startup in this
    case, don't silently allow it unnoticed).
  - Whether requests default to Gemini's "temporary chat" (not saved to account history)
    mode.
  - Tunables for the reliability features in §2.8: concurrent-generation cap, connection
    timeout, "zombie stream" watchdog timeout, cookie-rotation interval.
  - Local activity-log retention period (§2.7).
  - Warm-session idle timeout (§2.9).
- A minimal `.env`-style loader (`KEY=VALUE` lines, `#` comments, existing real
  environment variables always win over `.env` values) for secrets that shouldn't live
  in the JSON config — specifically the admin credential in §2.6.

### 2.2 Cookie-based authentication

- Two cookies matter for a Gemini Web session: a long-lived login cookie and a
  short-lived, frequently-rotating token. The exact cookie names are Google's, discover
  them from `gemini_webapi`'s own documentation/interface — do not guess or hardcode
  based on any other project's variable names.
- The full `google.com`-domain cookie set (not just `gemini.google.com`-scoped) must be
  accepted and passed through, because the session-refresh endpoint lives on a different
  subdomain (`accounts.google.com`) and needs cookies a narrower export wouldn't include.
- Cookie input formats to accept (a human will be pasting this from a browser
  extension's export, so be permissive): a JSON array of `{name, value, ...}` objects, a
  `{"cookie": "raw-string"}` object, or a raw `name=value; name=value` cookie-header
  string. Detect the shape rather than requiring the caller to specify it.
- Cookie loading must be cheap to call repeatedly (e.g. on every request that needs to
  check auth state) — cache file contents keyed by file modification time, not on a
  fixed interval, so a fresh import is picked up on the very next read.
- **No cookie configured must not be a hard error.** The underlying library has (or
  should be given, if not natively supported) a genuine anonymous/guest-session fallback
  using Gemini's free public tier. A service with zero configured credentials should
  still serve requests, just at anonymous-tier capability. This is a real, load-bearing
  requirement, not a nice-to-have — verify it works with an actual empty-credential
  request during implementation, not just by reading library documentation (see the
  pitfall about cache false-positives in §7).

### 2.3 OpenAI-compatible API

Endpoints required:

- `POST /v1/chat/completions` — standard OpenAI Chat Completions request/response shape.
  Must support: multi-turn `messages` arrays with `system`/`user`/`assistant`/`tool`
  roles; multimodal content parts (text + image, both `image_url` with a remote URL, and
  inline base64 data URLs); `tools`/`tool_choice` (OpenAI function-calling shape);
  `stream: true` via Server-Sent Events in OpenAI's chunked delta format.
- `POST /v1/responses` — OpenAI's newer Responses API shape (distinct request/response
  structure from Chat Completions — a flat `input` array rather than nested `messages`,
  different streaming event model with named event types like
  `response.output_text.delta`). Some agentic coding tools use this shape specifically
  instead of Chat Completions; both must be supported independently, not as aliases of
  each other.
- `GET /v1/models` — returns the model list live, by asking the authenticated account
  what it actually has access to (via `gemini_webapi`), not a hardcoded table. Different
  accounts (free vs. subscribed) legitimately see different models; a static list would
  misrepresent this.

**Message-to-prompt translation** (required, since Gemini Web takes one text prompt, not
structured messages): flatten the incoming message array into a single prompt string
that preserves conversational structure legibly (e.g. distinguishable per-role
sections), collect any images referenced into a separate list passed alongside the
prompt (not embedded in the text), and — when tools are declared — inject a clear
natural-language instruction block describing the available tools and the exact
plain-text format the model should use to "call" one (since Gemini Web has no native
function-calling protocol; this has to be prompt-engineered and then parsed back out of
the reply text).

**Tool-call parsing** (required): after receiving the model's plain-text reply, extract
any tool-call invocations matching the format instructed above, and return them in
OpenAI's structured `tool_calls` shape, with the tool-call syntax stripped from the
visible reply text. **Known correctness requirement, not optional**: the model will not
always terminate its tool-call block with a newline immediately before the closing
delimiter — a parser that requires a strict newline-then-delimiter match will silently
fail to recognize well-formed tool calls some fraction of the time, dumping the raw
syntax into the visible reply instead of a structured tool call. Test this specific case
explicitly (a tool-call block with no newline before its closing fence) before
considering tool-calling done.

### 2.4 Google-native API

- `POST /v1beta/models/{model}:generateContent` and `POST
  /v1beta/models/{model}:streamGenerateContent` — Google's own request/response shape:
  `contents` array (with `role: "user"|"model"`, `parts` containing `text` and/or
  `inlineData` for images), optional `systemInstruction`, optional `tools` with
  `functionDeclarations`, `toolConfig.functionCallingConfig.mode` for tool-choice
  control (`AUTO`/`ANY`/`NONE`, with `allowedFunctionNames` when `ANY`).
- `GET /v1beta/models` — live model list, same source as §2.3's `/v1/models`, reshaped
  into Google's `{"models": [{"name": "models/...", ...}]}` format.
- Same content-flattening and tool-call-parsing requirements as §2.3, adapted to
  Google's part/function-call/function-response shapes instead of OpenAI's. `inlineData`
  parts (base64 image data) must decode into the same image list mechanism used by the
  OpenAI path.
- Function-call parsing on this path should tolerate at least: a fenced code-block
  format, the same content without a fence, and (last resort) a reply that is *only* a
  bare JSON object with a name/args shape — because Gemini doesn't perfectly reproduce
  whatever exact format was requested every time, and treating anything less than the
  primary format as "no tool call" produces confusing failures.

### 2.5 Image handling (input and output)

- **Input**: accept images via remote URL (fetch server-side) or inline base64 data URL
  (decode directly). MIME type must be determined from the actual file signature (magic
  bytes) — common raster formats at minimum: PNG, JPEG, GIF, WEBP, BMP, TIFF, AVIF,
  HEIC. Do not trust a caller-supplied MIME type/extension without verification, since
  the upload path may depend on getting this right (some upload mechanisms only honor an
  extension from a real file path, not an in-memory buffer's metadata — if using
  `gemini_webapi`'s upload mechanism, confirm what it actually keys off of rather than
  assuming, and write to a real temp file with the correct extension if needed).
- **Output**: when Gemini's reply includes a generated or referenced image (not just
  text), the response must include that image, base64-encoded, directly in the API
  response — not merely a Google-hosted URL requiring this service's own session cookies
  to fetch (which is useless to an external caller). On the Google-native endpoint,
  generated images should also appear as native `inlineData` parts in the response
  content, matching how the real Gemini API represents image output — in addition to
  whatever custom metadata field carries the same data for the OpenAI-compatible path.
- A non-standard `images` field (or equivalent) should be included in every generation
  response (OpenAI-compatible responses have no native field for this) alongside
  whatever "which model actually served this" metadata (§2.6) already exists there.

### 2.6 Model resolution and response metadata

- Callers specify a model via a simple naming convention (e.g. a short prefix plus the
  real model family name) that maps onto whatever the live account's model registry
  calls it — do not hardcode a model-name-to-internal-ID table, since this drifts the
  moment the provider renames or adds models. Resolve against the account's live
  discovered model list at request time (via the underlying library), and return a clear
  4xx error listing the account's *actual* available models when an unknown name is
  requested — never silently substitute a different model.
- An extended-thinking / reasoning-effort toggle, expressed as a suffix on the model
  name (e.g. `-high` vs `-low`/`-medium`), mapped onto whatever binary or graded
  reasoning control the underlying library actually exposes. **Do not build a
  finer-grained reasoning-depth control than the underlying library actually supports**
  — if the library only exposes on/off, expose on/off; implementing a fake graded scale
  that doesn't correspond to a real upstream parameter is worse than not having it.
- **Every generation response must include which model actually served the request**,
  under a clearly-namespaced metadata field, derived from the same validated model
  resolution used to build the request — not inferred from the model's own text reply.
  **This is a hard requirement, and the reasoning matters**: a language model's own
  claim about its identity/version in conversational text is unreliable and not
  grounded in its actual deployment metadata — models routinely misstate their own
  identity. The only trustworthy signal is what your own resolution/routing code
  actually selected and validated against the live account, never what the model says
  about itself in a reply.
- Account quota/usage information (tier, usage windows, credits remaining) should be
  fetched live from the account and included in the same response metadata field, and
  separately exposed via the monitoring endpoint (§2.7).

### 2.7 Local observability

- **Machine-readable health endpoint** (`GET /status` or equivalent): must distinguish
  at least three states as *separate* signals, not collapsed into one boolean, because
  they fail independently and mean different things operationally:
  1. Whether a request to Gemini's own page/session-check succeeds at all (proves the
     cookie value itself isn't garbage).
  2. Whether the actual client library considers itself authenticated against a real
     account (this can be false even when #1 is true — a page can load successfully
     while the underlying session token has separately failed, silently dropping
     requests to anonymous/free-tier quality with zero error anywhere if this
     distinction isn't surfaced).
  3. Whether requests are actually completing successfully over some recent time window
     (see local request history, below) — auth can report healthy while every request
     is still failing or extremely slow for unrelated reasons.
  Report these three independently; do not let "the page loaded" stand in for "this is
  healthy."
- **Local request history**: log every generation request (model requested, model
  actually served, success/failure, latency, rough size) to a small local persistent
  store (a single-file embedded database is sufficient — avoid anything requiring a
  separate running service). Summarize this over a trailing window (e.g. 24 hours):
  total count, error count/rate, average latency, per-model breakdown, time since last
  request. Surface this summary on both the machine-readable endpoint and any
  human-facing dashboard (§2.9). Writes to this store must never block or slow down the
  actual request path — do them off the request-handling path entirely (e.g.
  fire-and-forget on a background task), and a failure to write must never fail the
  underlying request.
- **A human-facing status/recovery dashboard** (§2.9) is a separate requirement from the
  machine-readable endpoint, not a replacement for it.

### 2.8 Reliability under concurrent load

This section exists because of a real production incident during the reference
implementation's development — take it seriously, it is not speculative.

- **The problem**: this system's single shared upstream connection does not reliably
  handle many long-running generations submitted concurrently. Under load, observed
  behavior was: the connection stops receiving any data at all for several minutes, then
  breaks entirely, taking down every request that had piled onto it — some returning
  clean errors, some just hanging until a client-side timeout gives up. This is *not* a
  limitation of Gemini's backend (the real gemini.google.com web app handles multiple
  concurrent conversations fine — each browser tab is its own independent connection);
  it is specific to this system's single shared connection.
- **Required mitigation, two parts**:
  1. Cap how many generations can be in flight against the shared connection at once
     (a small number, e.g. 2-4, configurable) — a request beyond the cap waits for a
     slot rather than piling onto the connection unbounded. **Do not serialize to
     exactly 1** — this system is meant to back multiple independent callers
     simultaneously; a strict single-file queue would bottleneck all of them behind
     whichever request happens to be slowest.
  2. Shorten the underlying library's own connection/stream timeouts from whatever
     generous defaults it ships with, so a genuinely stuck request fails and frees the
     connection in a reasonable time instead of blocking everything behind it for many
     minutes.
- **Two rejected alternative designs, and why**: (a) opening multiple independent
  upstream sessions to mimic multiple browser tabs was considered and rejected — running
  multiple simultaneous authenticated sessions from one account/process is a plausible
  way to look more suspicious to the provider's own abuse detection, not less, and this
  system already has to be careful about that (see §7). (b) A queue-based full
  serialization was considered and rejected for the "bottlenecks unrelated callers"
  reason above.
- **A related, separate problem, not fixed by the above**: even a single, completely
  isolated request (nothing else running concurrently) can occasionally stall for
  several minutes before eventually succeeding, due to upstream connection flakiness
  unrelated to load. Since this can exceed what a typical client-side HTTP timeout will
  tolerate, be aware that "the request eventually succeeds server-side" and "the caller
  is still there to receive it" are different guarantees — a slow-but-successful
  response can complete into a connection nobody is listening to anymore. Document this
  tradeoff for whoever integrates against this service; a fully engineered fix (e.g. an
  async submit-then-poll job pattern instead of one long-held HTTP connection) is a
  reasonable future improvement but is not required for an initial implementation.

### 2.9 Admin/recovery dashboard

- A browser-accessible page that: (a) shows the same health signals as §2.7's
  machine-readable endpoint, formatted for human reading, and (b) accepts a pasted
  cookie export (any of the formats from §2.2) and applies it immediately — tearing down
  and re-initializing the underlying client so the new credentials take effect without a
  process restart. This exists specifically so recovering a broken session doesn't
  require shell/server access — a browser is enough.
- This page must require authentication. Do not ship it open by default, and do not
  require the operator to configure a credential before getting a protected instance —
  generate a random credential on first boot if none is explicitly configured, persist
  it locally (a file, correctly permissioned), and log it prominently at startup. Also
  support pinning an explicit credential via the `.env` mechanism from §2.1. Accept the
  credential via at least: HTTP Basic auth (so a browser's native prompt works with zero
  custom login page) and a header or query-param form (so non-browser monitoring tools
  can authenticate too).
- The generation endpoints (§2.3, §2.4) and the admin/status endpoints should use
  *separate* credential mechanisms — a caller with a generation API key should not
  thereby have admin access, and vice versa.
- Cookie import via a watched local file (in addition to the browser paste form) is a
  reasonable additional capability: poll a configured file path on an interval, and
  treat any content that appears there the same as a manual paste.

### 2.10 Optional: temporary/unlogged chat mode

- Gemini's own web app has a "temporary chat" mode that excludes a conversation from the
  account's saved chat history. Expose this as: a global config default, and a
  per-request override field on generation requests. Omitting the override field must
  fall back to the config default, not force a fixed value regardless of config.
- Document clearly (this matters, don't gloss over it) that this only controls the
  *provider's own* chat history — it has no bearing on whether this service's own local
  request history (§2.7) records that a request happened. If true end-to-end
  non-logging matters to an integrator, both need to be considered separately.

### 2.11 Optional: reusable warm chat sessions

- **Motivating requirement**: a cold, single-shot request (fresh conversation, prompt +
  attachment in one call) measurably costs significantly more latency than the same
  prompt sent as a follow-up turn in an already-established conversation — this is a
  real, measurable cost of provider-side per-conversation setup, not a hypothesis.
  Verify this cost exists in your own implementation empirically before building this
  feature, and again after, with real timing numbers, not assumed.
- **Design**: an opt-in endpoint to explicitly start a session (which must actually
  exchange at least one real message — constructing a session handle alone typically
  does not allocate anything on the provider side, only a real sent-and-answered message
  does), returning a session identifier. Generation requests can then optionally include
  that identifier to route through the already-established session instead of starting
  fresh.
- **This must be strictly opt-in.** A request that doesn't reference a session must
  behave exactly as if this feature didn't exist — one fresh, stateless conversation per
  call, unchanged default behavior.
- Session state should be held in memory only (no persistence requirement across
  restarts), pruned automatically after a configurable idle period, and explicitly
  invalidated whenever the underlying authenticated client is torn down and
  re-initialized (e.g. after a fresh cookie import) — a session tied to a now-dead
  client instance must not silently reference it.
- An unknown or expired session identifier referenced in a request must produce a clear
  error response, never a silent fallback to a fresh stateless conversation — the caller
  needs to be able to tell "start a new session" apart from "this got slower for no
  visible reason."
- Whatever model a session is associated with should be fixed for that session's
  lifetime if the underlying per-conversation object doesn't support changing it
  mid-conversation — verify this constraint against the actual library capability rather
  than assuming either way.

---

## 3. Non-functional requirements

- **No official Google API key or billing account required at any point.**
  Authentication is entirely cookie-based, reusing whatever access an already-signed-in
  Google account has.
- **Interactive API documentation** should be available for free if the chosen framework
  provides it out of the box (verify this rather than building a custom docs page).
- **Docker deployment** should be supported, with all local-file state (cookie file,
  admin credential file, local activity-log database, cookie-cache used by the
  underlying library's own auto-refresh) mounted as persistent volumes — a container
  recreate must not silently regress to stale credentials/state that the underlying
  library had already rotated past.
- **Every non-trivial reliability fix should be verified against live behavior**, not
  just unit-tested in isolation, and the verification method should guard against false
  positives (§7 has a specific example of this going wrong and how it was caught).

---

## 4. Suggested build phases

Each phase should be independently testable and left in a working state before moving
to the next. Order reflects dependency, not necessarily priority — adjust if a different
order makes more sense once implementation is underway.

1. **Foundation**: project scaffold, config loading (§2.1), a minimal health-check
   endpoint, `gemini_webapi` wired up with basic cookie loading (§2.2) including the
   anonymous-fallback requirement from day one (verify it, don't defer it).
2. **Core OpenAI-compatible chat**: `/v1/chat/completions` (non-streaming first, then
   streaming), `/v1/models`, message-to-prompt translation, live model resolution and
   the served-model response metadata (§2.6).
3. **Google-native parity**: `/v1beta` endpoints, content translation, sharing as much
   of the underlying generation plumbing with phase 2 as makes sense.
4. **Multimodal**: image input (both remote URL and inline base64), then image output
   (§2.5).
5. **Tool/function calling**: both wire formats, including the fence-tolerance
   requirement called out in §2.3 — write a test for that specific edge case, don't
   assume it away.
6. **`/v1/responses`**: the distinct OpenAI Responses API shape, including its own
   streaming event model.
7. **Observability**: local request history, the three-way health-signal split (§2.7).
8. **Admin dashboard + auth**: cookie-paste recovery, self-generated credential,
   separate from generation-endpoint auth (§2.9).
9. **Reliability hardening**: concurrency cap and shortened timeouts (§2.8) — ideally
   validated against an actual concurrent-load test, not just code review, since the
   original motivating problem only appeared under real concurrent load.
10. **Optional features**: temporary chat (§2.10), warm sessions (§2.11) — build and
    measure the warm-session latency claim before committing to shipping it.
11. **Polish**: any human-facing dashboard visual design, README, deployment docs.

---

## 5. Explicit non-requirements (do not build these)

- A graded, multi-level reasoning-depth control beyond whatever the underlying library
  natively exposes (see §2.6). This was deliberately dropped in the reference
  implementation rather than faked.
- A full historical-metrics dashboard with charts (beyond the summary described in
  §2.7) — reasonable future work, not part of an initial build.
- Multi-account support (running several Google accounts side by side, with manual or
  automatic failover) — a reasonable future direction, substantial enough scope that it
  should be its own follow-up effort, not bundled into an initial implementation.

---

## 6. Acceptance checklist

Before considering an implementation complete, confirm each of these against the real,
running service (not just code review):

- [ ] A request with zero configured cookies succeeds via anonymous/guest access —
      tested with an isolated environment so any local session-cache mechanism can't
      produce a false positive (see §7).
- [ ] A request for an unknown model name returns a clear error listing real available
      models, not a silent substitution.
- [ ] The served-model response field reflects actual validated routing, confirmed by a
      live call, not the model's own text claim about its identity.
- [ ] A tool-call reply with no newline directly before its closing delimiter still
      parses correctly.
- [ ] Multiple concurrent generation requests (more than the configured cap) queue
      rather than piling onto the connection unbounded, and the system recovers cleanly
      afterward.
- [ ] The admin dashboard requires authentication out of the box with zero manual setup,
      and the generated credential is discoverable (log + file).
- [ ] A cookie paste through the admin dashboard takes effect without a process restart.
- [ ] `/status` (or equivalent) reports degraded when the underlying client isn't
      actually authenticated, even if the page-load check alone would report healthy.
- [ ] If warm sessions are implemented: an unknown session ID returns a clear error, not
      a silent fresh-conversation fallback.

---

## 7. Known pitfalls (learned the hard way — read before implementing the related section)

- **Silent model downgrades** are the single most important failure mode to design
  against. Whatever mechanism the provider actually uses to pick a model per-request is
  probably not a small guessable integer or static ID — treat "is this model actually
  available to this account" as a live question answered by the account itself, not a
  fixed table, from the start.
- **A page loading successfully does not mean the underlying client is authenticated.**
  These are different signals that fail independently (§2.7). Conflating them is how a
  system ends up reporting "healthy" while silently serving anonymous-tier responses.
- **A cached session from a previous run can produce a false positive when testing
  anonymous mode.** If the underlying library caches successful sessions locally
  (independent of your own config), testing "does zero-credentials access actually
  work" from a machine that has ever successfully authenticated before may silently
  reuse that cached session instead of genuinely testing the credential-free path. Use
  an isolated cache location when verifying this specific behavior.
- **Concurrent requests can break a shared connection in ways that look like individual
  request failures** rather than an obvious systemic problem — if failures cluster in
  time and share a similar hang-then-fail signature, suspect the shared-connection
  concurrency issue (§2.8) before assuming each failure is independent and unrelated.
- **A model's self-reported identity in conversational text is not a reliable signal**
  of what was actually served (§2.6) — this will come up as a confusing "is my routing
  broken?" moment at some point; the answer is almost always "no, the model just
  misstated itself," verifiable against the response's actual served-model metadata
  field or a fresh direct API call.
- **Automating a personal account's session carries real account-level risk** —
  aggressive/repeated cookie rotation probing, many rapid re-authentications, or heavy
  concurrent load in a short window have been observed (in the reference
  implementation's own development) to push a real account into a degraded,
  hard-to-diagnose authentication state that took time to resolve. Treat the account
  behind this system as shared, sensitive state, not a disposable test fixture — don't
  script aggressive retry loops against it, and if something looks broken, prefer
  waiting and re-checking cold over immediately retrying.
- **Browser choice affects cookie export longevity.** Recent Chromium-based browsers
  (Chrome, Edge, Brave) can bind an exported session to the exporting browser/device in
  a way that expires it within hours, unrenewable by this system. This is a
  browser/platform behavior, not something fixable in this system's code — document it
  as operational guidance (recommend Firefox for cookie export) rather than trying to
  work around it.
