# API Reference

This document describes every HTTP endpoint this service exposes: what it's
for, what to send it, and what you get back. It's organized in the order
you'd actually need it: how auth works, how to pick a model, then the
endpoints themselves grouped by what they're for.

Base URL in every example below: `http://localhost:8000` (or wherever you've
deployed it).

If you'd rather explore interactively than read, FastAPI generates live,
always-current docs for every endpoint automatically: **Swagger UI** at
`/docs`, **ReDoc** at `/redoc`. Both come from the same code as this
document, so they can't drift out of sync with the real behavior the way a
hand-written doc can.

---

## Contents

**Before you send a request**
- [Authentication](#authentication)
- [Choosing a model](#choosing-a-model)
- [Response metadata (`x_gemini_proxy`)](#response-metadata-x_gemini_proxy)
- [Errors](#errors)

**Chat and generation**
- [OpenAI-compatible API](#openai-compatible-api)
  - [`GET /v1/models`](#get-v1models)
  - [`POST /v1/chat/completions`](#post-v1chatcompletions)
  - [`POST /v1/responses`](#post-v1responses)
- [Tool (function) calling](#tool-function-calling)
- [Google-native API](#google-native-api)
  - [`GET /v1beta/models`](#get-v1betamodels)
  - [`GET /v1beta/models/{model}`](#get-v1betamodelsmodel)
  - [`POST /v1beta/models/{model}:generateContent`](#post-v1betamodelsmodelgeneratecontent)
  - [`POST /v1beta/models/{model}:streamGenerateContent`](#post-v1betamodelsmodelstreamgeneratecontent)

**Extras**
- [Warm sessions (reusing a conversation)](#warm-sessions-reusing-a-conversation)
- [Admin dashboard](#admin-dashboard)

**Operations**
- [Health and monitoring endpoints](#health-and-monitoring-endpoints)
  - [`GET /healthz`](#get-healthz)
  - [`GET /status`](#get-status)
- [Implementation status](#implementation-status)

---

## Authentication

There are **two completely separate credential systems** in this service.
Mixing them up is the most common source of confusion, so it's worth being
explicit up front:

| | Generation API keys (`api_keys`) | Admin credential |
|---|---|---|
| Protects | `/v1/*` and `/v1beta/*` (the actual chat/generation endpoints) | `/admin`, `/admin/status.json`, `/admin/cookies` |
| Configured via | the `api_keys` list in `config.json` | `admin_username` + `ADMIN_PASSWORD`, see the README's [Admin dashboard: username and password](../README.md#admin-dashboard-username-and-password) |
| Grants access to the other? | No | No |

This section covers the first one, generation API keys. For the admin
credential, see [Admin dashboard](#admin-dashboard) below.

### Generation API keys

| Config state | Behavior |
|---|---|
| `api_keys` is a non-empty list | a key is **required** on every `/v1/*` and `/v1beta/*` request |
| `api_keys` is empty (the default) | those endpoints are **open** to anyone who can reach them; a warning is logged at startup so this isn't silently unnoticed |

If you've set `api_keys`, supply one using any of these (checked in this
order, so the first one present wins):

| Form | Example |
|---|---|
| Bearer token | `Authorization: Bearer <key>` |
| `x-api-key` header | `x-api-key: <key>` |
| `x-goog-api-key` header | `x-goog-api-key: <key>` |
| query parameter | `?key=<key>` |

A missing or wrong key gets a `401` with `{"detail": "..."}`.

`GET /healthz` never needs auth, it's a pure liveness check with no
account or usage detail in it. `GET /status`, on the other hand, along with
the interactive docs below, are gated behind the **admin credential** by
default, not the generation `api_keys`, same login as `/admin`.

### `/docs`, `/redoc`, `/openapi.json`, and `/status` are configurable

These four routes expose more than a simple health check: `/status` reports
account health, cookie state, and usage; `/docs`, `/redoc`, and
`/openapi.json` expose your entire API surface (every route, every
request/response schema, including the admin endpoints' shapes), and
Swagger UI's "Try it out" button lets a visitor send real requests directly
from the browser. Because of that, both are gated behind the admin
credential by default rather than left open.

This is controlled by two config keys (also settable as environment
variables, `GEMINI_PROXY_DOCS_ACCESS` / `GEMINI_PROXY_STATUS_ACCESS`, see
the README's [Environment variables](../README.md#environment-variables)):

| Key | Default | Values |
|---|---|---|
| `docs_access` | `"admin"` | `"admin"` (gated behind the admin credential), `"open"` (no auth, FastAPI's stock behavior), `"disabled"` (the routes don't exist, `404`) |
| `status_access` | `"admin"` | same three values, for `GET /status` |

If `api_keys` is set, requests against the generation endpoints still need
a valid key regardless of `docs_access`, gating `/docs` doesn't change that
separately, it just controls whether your API's structure and `/status`'s
account detail are visible to begin with.

---

## Choosing a model

You don't need to memorize model IDs. Send whatever name makes sense
(`gemini-flash`, `gemini-pro`, `gemini-2.5-flash`, a display name, even a
raw model ID hex), and the proxy resolves it against **the live list your
signed-in account actually has access to**. There's no hardcoded table of
model names anywhere in this service; if Google renames or adds a model
tomorrow, this proxy notices it the next time it asks, it doesn't need a
code change to catch up.

Not sure what your account actually has? Call `GET /v1/models` or
`GET /v1beta/models` and read the real names back.

### Turning on extended thinking

Append a suffix to the model name to control Gemini's "extended thinking"
(deeper reasoning before answering):

| Suffix | Extended thinking |
|---|---|
| `-high` | **on** |
| `-medium`, `-low`, `-minimal`, `-none` | off (written out explicitly, same as no suffix) |
| *(no suffix)* | off |

Examples: `gemini-pro-high` turns it on, `gemini-flash` leaves it off.

This is deliberately on/off, not a graded scale (like "low/medium/high"
actually doing different things). The underlying library only exposes a
single true/false switch, and faking a finer-grained control that doesn't
correspond to anything real would just be misleading.

### If the model name doesn't resolve

Two different failure cases, both returned as a `400` **before** any request
ever reaches Gemini, so you're not billed latency for a request that was
always going to fail:

- **The name matches nothing at all** on your account: `code: "model_not_found"`,
  with the real list of models you can use included in the error.
- **The name resolves, but you can't actually use it** (e.g. asking for a
  non-default model while running on the free anonymous/guest tier):
  `code: "model_unavailable"`.

The proxy never silently swaps in a different model than the one you asked
for. If it can't honor your request, it tells you, it doesn't guess.

### If you don't specify a model

The `default_model` from `config.json` is used (ships as `gemini-flash`).

---

## Response metadata (`x_gemini_proxy`)

Every successful generation response, on either wire format, streaming or
not, carries an `x_gemini_proxy` object. On a streaming response it rides
along on the final chunk.

This object exists for one specific reason: **a language model's own claims
about its identity in a conversational reply are not trustworthy.** Models
routinely misstate their own version or name in plain text. So instead of
parsing "I am Gemini 2.5" out of the reply, this field reports what your
request was *actually, verifiably* routed to, straight from the resolution
logic that picked it, not from anything the model said about itself.

| Field | Meaning |
|---|---|
| `requested_model` | the exact string you sent, suffix included |
| `served_model` | the model actually used, from validated resolution, **not** parsed from the reply text |
| `model_id` | Gemini's internal hex ID for the served model |
| `extended_thinking` | whether extended thinking was enabled for this request |
| `cookie_mode` | `authenticated`, `anonymous`, or `anonymous(forced)` |
| `chat_metadata` | Gemini's own conversation IDs (`[cid, rid, ...]`) |
| `usage_info` *or* `quotas` | a live snapshot of your account's usage/quota (whichever the underlying library provides) |
| `input_image_errors` | only present if one or more images you sent were skipped; a list of why |
| `output_image_count` | only present if the reply itself produced images |

**Trust `served_model`, not the model's own words about itself.**

---

## Errors

Errors are returned as structured objects with a `code` you can branch on
in your own code, not just a human-readable message you'd have to
string-match.

**On the OpenAI-compatible surface** (`/v1/*`), errors use OpenAI's own
envelope shape:

```json
{
  "error": {
    "message": "…",
    "type": "invalid_request_error | upstream_error | internal_error",
    "param": "model",
    "code": "model_not_found",
    "available_models": ["gemini-flash-lite", "…"]
  }
}
```

**On the Google-native surface** (`/v1beta/*`), errors use Google's own
envelope shape instead, matching whatever tooling you have that already
expects real Google API errors:

```json
{
  "error": {
    "code": 400,
    "message": "…",
    "status": "INVALID_ARGUMENT",
    "availableModels": ["models/gemini-flash-lite", "…"]
  }
}
```

### Error codes

| `code` | HTTP | What it means |
|---|---|---|
| `model_not_found` | 400 | The model name you sent matches nothing on your account. |
| `model_unavailable` | 400 | The model resolves, but your account/tier can't use it right now. |
| `session_unauthenticated` | 403 | No valid cookies configured; anonymous/guest sessions can only use the default model. |
| `usage_limit_exceeded` | 429 | You've hit your account's own compute/usage cap. |
| `rate_limited` | 429 | Gemini is temporarily rate-limiting this client. |
| `capacity` | 503 | This proxy's own concurrency cap (`max_concurrent_generations`) is full and no slot freed up in time; retry shortly. |
| `upstream_timeout` / `request_timeout` | 504 | The request to Gemini stalled past the configured timeout budget. |
| `upstream_error` | 502 | Any other upstream failure not covered above. |

### Concurrency and timeouts

This proxy holds **one shared connection** to your Gemini account, so it
caps how many generations can be in flight at once
(`max_concurrent_generations`, default 3). A request beyond that cap waits
up to `slot_wait_timeout` for a slot to free up, then gets `503 capacity`
rather than piling on top of an already-busy connection. A single
non-streaming generation also has its own hard ceiling, `request_timeout`
(default 180s, then `504 request_timeout`).

**Worth knowing if you're integrating this into something:** Gemini Web
itself occasionally stalls for minutes even when nothing else is running
against it. "The request eventually succeeded on the server" and "the
client is still there, waiting to receive it" are two different guarantees,
a slow-but-successful reply can complete into a connection your own client
already gave up on. Set generous client-side timeouts, or prefer
non-streaming requests with a retry on timeout. You can also watch
`GET /status`'s `capacity` object (`{limit, in_flight, waiting,
rejected_total}`) to see this in real time.

On a **streaming** request specifically, the HTTP response has already
started (`200`, headers sent) by the time an error can occur mid-stream, so
the error is emitted as the last SSE event instead of an HTTP status code,
followed by the normal stream terminator. Expected/classified errors are
logged at `WARNING` with no stack trace; anything genuinely unexpected gets
a full traceback in the logs.

---

## OpenAI-compatible API

Use this surface if you already have code written against OpenAI's API, an
SDK, LangChain, an agent framework, whatever. Point it at
`http://localhost:8000/v1` with any API key (or a real one from `api_keys`
if you've set that), and it should work with little to no changes.

### `GET /v1/models`

Returns the live model list for your authenticated account, in OpenAI's
model-list shape (with a `gemini` object appended for the extra detail
OpenAI's own shape has no room for).

**Parameters:** none (generation auth still applies).

**Response `200`:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "gemini-flash",
      "object": "model",
      "created": 1788240000,
      "owned_by": "google-gemini-web",
      "gemini": {
        "model_id": "56fdd199312815e2",
        "display_name": "3.7 Flash",
        "description": "…",
        "aliases": ["flash", "gemini-3.7-flash", "…"],
        "is_available": true
      }
    }
  ]
}
```

`503` if the Gemini client itself can't be initialized (e.g. bad or missing
cookies and anonymous fallback also failing).

---

### `POST /v1/chat/completions`

The endpoint you'll use for almost everything: OpenAI's Chat Completions
shape, including multi-turn conversations, image attachments, streaming,
and tool calling.

**Request body:**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `messages` | array | **yes** | — | non-empty; see message shape below |
| `model` | string | no | `default_model` | see [Choosing a model](#choosing-a-model) |
| `stream` | boolean | no | `false` | stream the reply as SSE when `true` |
| `temporary_chat` | boolean | no | `temporary_chat_default` | when `true`, this conversation is **not** saved to your Gemini account's own chat history (this is separate from, and doesn't affect, this service's own local request-history log) |
| `session_id` | string | no | — | route this request through an existing [warm session](#warm-sessions-reusing-a-conversation) instead of starting fresh; fixes the model to that session's, and returns `409` if the session ID is unknown |
| `tools` | array | no | — | OpenAI function-calling shape: `[{"type":"function","function":{"name","description","parameters"}}]`. See [Tool (function) calling](#tool-function-calling) for how this actually works under the hood. |
| `tool_choice` | string / object | no | `"auto"` | `"auto"`, `"none"` (don't even offer tools this turn), `"required"` or `{"type":"function","function":{"name":"…"}}` (push for a specific call) |

Other standard OpenAI fields (`temperature`, `top_p`, `max_tokens`, `n`,
`stop`, `seed`, `response_format`, …) are accepted without erroring, but
currently ignored, Gemini Web has no equivalent knobs to forward them to.

**Message shape:**

| Field | Notes |
|---|---|
| `role` | `system`, `user`, `assistant`, `tool`, `function`, or `developer` (treated the same as `system`) |
| `content` | either a plain string, or an array of typed parts (below) |
| `content[]` text part | `{"type": "text", "text": "…"}` (also accepts `input_text` / `output_text`, or really any part that has a `text` key) |
| `content[]` image part | `{"type": "image_url", "image_url": {"url": "<http(s) or data: URL>", "detail": "…"}}`. The image is fetched (for an `http(s)` URL) or decoded (for a `data:` URL) server-side and attached to the prompt. The real MIME type is sniffed from the file's own bytes, not trusted from whatever you claim; a caller-supplied type is ignored. If one image fails to fetch or isn't recognized, that single image is skipped (the request still proceeds) and the reason shows up in `x_gemini_proxy.input_image_errors`. |
| `tool_calls` (on an assistant message) | rendered into the prompt as a readable text trace, so the model can see its own prior call |
| `name` / `tool_call_id` (on a tool/function message) | prefixed onto the content in that same trace |

All messages get flattened into a single prompt Gemini actually understands,
with `Role:` section headers marking where each turn starts, preserving
turn order.

**Response `200` (non-streaming):**

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "created": 1788240000,
  "model": "gemini-flash",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "…" },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 123,
    "total_tokens": 123,
    "note": "token counts are rough estimates; Gemini Web reports none"
  },
  "x_gemini_proxy": { "…": "see Response metadata above" }
}
```

Token counts are a rough `len(text) // 4` estimate, Gemini Web's web
interface doesn't report real token usage, so don't rely on these for
billing-accurate math.

**Getting images back.** If Gemini's reply contains a generated or
referenced image, it's downloaded through your authenticated session (so
you don't need your own cookies to fetch it) and returned to you already
base64-encoded, both as a top-level `images` array and mirrored onto
`choices[0].message.images`:

```json
"images": [
  { "type": "image", "mime_type": "image/png", "data": "<base64>", "source_url": "https://…" }
]
```

**Response `200` (streaming, `stream: true`):**

`Content-Type: text/event-stream`. You'll receive a sequence of lines like:

```
data: {"id":"chatcmpl-…","object":"chat.completion.chunk","choices":[{"index":0,"delta":{…},"finish_reason":null}]}
```

The first chunk carries `delta: {"role": "assistant", "content": ""}` to
announce the turn is starting; subsequent chunks carry
`delta: {"content": "…"}` with the next bit of text; the final chunk has
`finish_reason: "stop"`, an empty `delta`, and the `x_gemini_proxy` object
attached. The whole stream is terminated by `data: [DONE]`, same convention
as OpenAI's own API.

---

### `POST /v1/responses`

OpenAI's newer **Responses API**. This is a genuinely separate wire shape
from Chat Completions (a flat `input` array instead of nested `messages`, a
different streaming event model), not just an alias for the endpoint above.
Some agentic coding tools and newer SDKs speak only this shape, which is
why it's implemented independently rather than translated through Chat
Completions internally.

**Request body:**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `input` | string **or** array | **yes** | — | a plain string, or an array of typed items (below) |
| `model` | string | no | `default_model` | see [Choosing a model](#choosing-a-model) |
| `instructions` | string | no | — | acts as the system prompt |
| `stream` | boolean | no | `false` | stream as named SSE events (below) |
| `tools` | array | no | — | Responses' flat shape: `[{"type":"function","name","description","parameters"}]` (not nested like Chat Completions) — see [Tool (function) calling](#tool-function-calling) |
| `tool_choice` | string / object | no | `"auto"` | `"auto"` / `"none"` / `"required"` / `{"type":"function","name":"…"}` |
| `temporary_chat` | boolean | no | `temporary_chat_default` | keep this conversation out of your Gemini account's own chat history |

Other Responses-specific fields (`temperature`, `max_output_tokens`,
`previous_response_id`, `parallel_tool_calls`, `metadata`, `reasoning`,
`store`, …) are accepted without erroring but currently ignored
(`metadata` is the one exception, it's echoed straight back to you
unchanged). `previous_response_id`-based conversation chaining isn't
implemented, send the prior turns yourself as part of `input` instead.

**`input[]` item shapes:**

| Item | Notes |
|---|---|
| `{"type":"message","role","content"}` | `content` is either a string or an array of parts: `{"type":"input_text"\|"output_text","text"}`, `{"type":"input_image","image_url"}` |
| `{"role","content"}` | shorthand for a message item, same as above without the explicit `"type":"message"` |
| `{"type":"function_call","name","arguments","call_id"}` | represents a prior tool call, rendered into the prompt so the model has context |
| `{"type":"function_call_output","call_id","output"}` | represents a prior tool result, likewise rendered into the prompt |
| a bare string | treated as shorthand for a user message |

**Response `200` (non-streaming):**

```json
{
  "id": "resp_…",
  "object": "response",
  "created_at": 1788248000,
  "status": "completed",
  "model": "gemini-flash",
  "output": [
    { "type": "message", "id": "msg_…", "status": "completed", "role": "assistant",
      "content": [{ "type": "output_text", "text": "…", "annotations": [] }] }
  ],
  "output_text": "…",
  "usage": { "input_tokens": 0, "output_tokens": 12, "total_tokens": 12 },
  "x_gemini_proxy": { "…": "see Response metadata above" }
}
```

Any tool calls the model makes show up as additional items in `output`:
`{"type":"function_call","id":"fc_…","call_id":"call_…","name":"…","arguments":"<json string>","status":"completed"}`.

**Response `200` (streaming):** `text/event-stream`, with named events
rather than raw deltas:

```
event: response.created            → { response: {…, status:"in_progress"} }
event: response.in_progress
event: response.output_item.added  → item stub (message / function_call)
event: response.content_part.added
event: response.output_text.delta  → { delta: "…" }        (repeated)
event: response.output_text.done   → { text: "…full…" }
event: response.content_part.done
event: response.output_item.done   → completed item
event: response.function_call_arguments.delta / .done       (for tool calls)
event: response.completed          → { response: {…full…, status:"completed"} }
```

If something fails mid-stream, it arrives as `event: response.failed` with
a `response.error` object, rather than a plain HTTP error.

---

## Tool (function) calling

Gemini Web, the consumer web app this service authenticates to, has **no
native function-calling protocol** at all, there's no API parameter you can
set to make it emit structured calls the way the real Gemini API or OpenAI
do. So this is built entirely by prompt engineering, in two steps:

1. Your `tools` list and a specific plain-text call syntax get injected
   into the prompt Gemini actually sees.
2. The reply is scanned for that syntax; anything matching is extracted
   into the proper structured shape for whichever wire format you're using,
   and stripped out of the visible reply text.

Both wire formats (OpenAI and Google-native) go through this same
mechanism, they just present the result in their own respective shapes.

**Reliability, honestly stated:** because this is prompt-driven rather than
a real API contract, whether the model actually emits a tool call (instead
of just answering in prose) isn't guaranteed. It depends on the model, how
you phrased the request, and `tool_choice`. Setting `tool_choice: "required"`
(or `mode: "ANY"` on the Google side) pushes hard for a call, but Gemini can
still occasionally just answer inline anyway. Your integration should treat
"no tool call, just a plain text answer" as a real possible outcome, not an
error case.

**What counts as a valid call, in order of preference:**

- A fenced code block using ` ```tool_call `, ` ```json `, or ` ```tool_code `
  containing `{"name": "...", "arguments": {...}}`, **including when the
  model forgets the newline right before the closing ` ``` `** (this is a
  known Gemini quirk that's specifically handled, not an edge case that
  slipped through).
- Multiple such blocks in one reply produce multiple tool calls.
- As a last resort, a bare top-level JSON object with a `name` plus an
  `arguments`/`args` field, in case the model skips the fence entirely.

**OpenAI-side response shape** — tool calls show up under
`choices[0].message.tool_calls`:

```json
{ "message": { "role": "assistant", "content": null,
    "tool_calls": [{ "id": "call_…", "type": "function",
      "function": { "name": "get_weather", "arguments": "{\"city\": \"Paris\"}" } }] },
  "finish_reason": "tool_calls" }
```

`arguments` is a JSON-encoded **string**, matching OpenAI's own convention
(not a nested object). `content` is `null` when the reply was purely a tool
call with no other text; otherwise it carries whatever prose the model kept
around it.

**Streaming with tools:** intermediate text deltas are held back while a
call block is being assembled (since it spans several chunks), then a
single `delta.tool_calls` frame is emitted once it's complete, followed by
the `finish_reason: "tool_calls"` frame.

**Continuing the conversation after you've run the tool:** send back both
the assistant message that contained the `tool_calls`, and a follow-up
message `{"role": "tool", "tool_call_id": "...", "content": "<your result>"}`.
Both get rendered into the prompt so the model actually sees what happened
when it "called" the tool.

**Google-side response shape** — a `functionCall` part inside
`candidates[0].content.parts`:

```json
{ "parts": [{ "functionCall": { "name": "get_weather", "args": { "city": "Paris" } } }] }
```

`toolConfig.functionCallingConfig.mode` maps onto the behavior above:
`AUTO` → normal auto behavior, `ANY` → push hard for a call (optionally
restricted via `allowedFunctionNames`), `NONE` → don't even inject the tool
list this turn.

---

## Google-native API

Use this surface if your tooling already expects Google's own Generative
Language API shape (`generativelanguage.googleapis.com`, `v1beta`) rather
than OpenAI's, for example, something built directly against the real
Gemini API that you want to point at this proxy instead without rewriting
its request/response handling.

### `GET /v1beta/models`

The live model list, in Google's own shape.

**Parameters:** none (generation auth still applies).

**Response `200`:**

```json
{
  "models": [
    {
      "name": "models/gemini-flash",
      "baseModelId": "gemini-flash",
      "displayName": "3.7 Flash",
      "description": "…",
      "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
      "gemini": { "modelId": "56fdd199312815e2", "aliases": ["…"], "isAvailable": true }
    }
  ]
}
```

---

### `GET /v1beta/models/{model}`

Look up a single model by name. `{model}` may be given with or without the
`models/` prefix (`gemini-flash` or `models/gemini-flash` both work), and
aliases are accepted too.

**Response `200`:** a single model object, same shape as one entry from the
list above. `404` if the name matches nothing on your account.

---

### `POST /v1beta/models/{model}:generateContent`

The main Google-shaped generation endpoint. `{model}` comes from the URL
path itself (with or without the `models/` prefix, and an optional
reasoning suffix works here too, e.g. `models/gemini-pro-high:generateContent`).

**Request body:**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `contents` | array or object | **yes** in practice | — | the conversation's turns; a single object (one turn) is also accepted, not just an array |
| `systemInstruction` / `system_instruction` | object or string | no | — | either `{"parts":[{"text":"…"}]}` or just a bare string |
| `temporaryChat` | boolean | no | `temporary_chat_default` | non-standard extension to Google's real API; excludes this chat from your account's own history |
| `tools` | array | no | — | `[{"functionDeclarations":[{"name","description","parameters"}]}]`, prompt-injected the same way as the OpenAI surface, see [Tool (function) calling](#tool-function-calling) |
| `toolConfig` | object | no | — | `functionCallingConfig.mode` (`AUTO`/`ANY`/`NONE`) plus optional `allowedFunctionNames` |
| `generationConfig`, `safetySettings` | — | no | — | accepted without erroring, but currently ignored |

**`contents[]` shape:**

| Field | Notes |
|---|---|
| `role` | `user` or `model` (also accepts `function` / `tool`, rendered as "Tool result") |
| `parts[]` | an array of part objects, described below |
| text part | `{"text": "…"}` |
| inline image | `{"inlineData": {"mimeType": "image/png", "data": "<base64>"}}` (snake_case `inline_data` also accepted). Decoded and attached; the MIME type is re-sniffed from the actual bytes rather than trusted as declared. |
| file reference | `{"fileData": {"fileUri": "https://…"}}`, fetched server-side and attached, same as an image URL on the OpenAI surface |
| `functionCall` | `{"functionCall": {"name": "…", "args": {…}}}`, rendered into the prompt as a readable trace |
| `functionResponse` | `{"functionResponse": {"name": "…", "response": {…}}}`, likewise rendered as a trace |

`systemInstruction` and every turn get flattened into one prompt Gemini
understands, with `System:` / `User:` / `Assistant:` section headers.

**Response `200`:**

```json
{
  "candidates": [
    {
      "content": { "role": "model", "parts": [{ "text": "…" }] },
      "finishReason": "STOP",
      "index": 0
    }
  ],
  "usageMetadata": {
    "promptTokenCount": 0,
    "candidatesTokenCount": 123,
    "totalTokenCount": 123
  },
  "modelVersion": "gemini-flash",
  "x_gemini_proxy": { "…": "see Response metadata above" }
}
```

`candidatesTokenCount` is a rough estimate, same caveat as the OpenAI
surface's `usage`.

**Getting images back.** Generated images appear two ways at once: as
native `inlineData` parts inside `candidates[0].content.parts` (matching
how the real Gemini API represents image output), **and** as a top-level
`images` array (`[{"mimeType","data","sourceUrl"}]`) for convenience.

---

### `POST /v1beta/models/{model}:streamGenerateContent`

Same request body as `:generateContent` above; the only difference is how
the response is delivered.

**Query parameters:**

| Param | Values | Effect |
|---|---|---|
| `alt` | `sse` | frame the stream as Server-Sent Events (`data: {…}\n\n`, `Content-Type: text/event-stream`) |
| *(omitted)* | — | frame the whole response as a single JSON array of chunk objects instead (`Content-Type: application/json`) |

Each chunk looks like:

```json
{ "candidates": [{ "content": { "role": "model", "parts": [{ "text": "<delta>" }] }, "index": 0 }],
  "modelVersion": "gemini-flash" }
```

The final chunk carries `candidates[0].finishReason: "STOP"`, empty
`parts`, the full `usageMetadata`, and the `x_gemini_proxy` object.

If something fails mid-stream, it's emitted as a `{"error": {…}}` element
(as a `data:` line for SSE, or as the last element in the array for the
JSON-array framing).

---

## Warm sessions (reusing a conversation)

**The problem this solves:** starting a brand-new conversation with Gemini
costs real, measurable setup time on top of however long generating the
actual reply takes. A follow-up message inside an *already-established*
conversation skips that setup cost entirely. Warm sessions let you pay that
setup cost once, explicitly, and then reuse the resulting conversation for
several requests instead of paying it on every single call.

**This is entirely opt-in.** If you never send a `session_id`, this
feature might as well not exist, every request behaves exactly like a
fresh, stateless, one-shot conversation, same as before this feature was
added.

### Managing sessions

| Endpoint | What it does |
|---|---|
| `POST /v1/sessions` | Starts a session. Body: `{model?, priming_message?}`. This resolves the model, opens a real chat, and **sends one real message** to it, a session handle by itself allocates nothing on Gemini's side; only an actual sent-and-answered message does. Returns `{session_id, model, created_at, last_used_at, turns, idle_seconds}`. |
| `GET /v1/sessions` | Lists your current sessions: `{object:"list", data:[…]}` |
| `GET /v1/sessions/{id}` | Info about one session; `404` if it's unknown or has expired |
| `DELETE /v1/sessions/{id}` | Closes a session immediately |

All four require a generation `api_key`, same as the chat/generation
endpoints do.

### Using a session

Add `"session_id": "sess_…"` to a `/v1/chat/completions` or `/v1/responses`
request body, or `"sessionId"` to a Google `generateContent` body. Once
you do:

- **The model is fixed** to whatever the session was created with, any
  `model` you also send in the request is ignored.
- **An unknown, expired, or invalidated session ID returns `409`** with a
  clear error, it never silently falls back to a fresh conversation. If
  your session died, you'll know, rather than just noticing things got
  slower for no obvious reason.
- A session is invalidated whenever the underlying Gemini client is rebuilt
  (a new cookie import, the cookie watcher picking up a change), after
  `warm_session_idle_timeout` of no use, or when it's evicted for being the
  least-recently-used past `max_warm_sessions`.
- Session state lives in memory only, it does not survive a process
  restart.
- For the best effect, send only the **new** turn(s) in
  `messages`/`input`/`contents`, the session already remembers everything
  that came before; resending the whole history defeats the purpose.

> The latency benefit here is real, this is a measured effect of how
> Gemini's per-conversation setup works, not a guess. It has not, however,
> been formally re-benchmarked in this specific deployment. Whether you use
> warm sessions or not has no effect on response correctness either way,
> it's purely a latency optimization.

---

## Admin dashboard

A browser-based page for recovering a broken session (most commonly: your
cookies expired) without needing shell or server access at all, just a
browser. It sits behind its **own credential**, entirely separate from the
generation `api_keys` covered under [Authentication](#authentication), a
generation key grants zero admin access, and the admin credential grants
zero generation access.

For where to find the admin username and password, see the README's
[Admin dashboard: username and password](../README.md#admin-dashboard-username-and-password)
section, it walks through exactly where to look.

**The credential can be presented in any of these ways:**

- HTTP Basic auth: `Authorization: Basic base64(admin:<password>)` (this is
  what makes your browser's native login prompt work automatically)
- an `X-Admin-Password` or `X-Admin-Key` header
- a `?admin_key=<password>` or `?admin_password=<password>` query parameter
- the `gop_admin` cookie, set automatically once any of the above succeeds,
  so the dashboard's own background requests stay authenticated without
  re-sending credentials every time (`SameSite=Strict`, `HttpOnly`, expires
  after 24h)

### Endpoints

| Endpoint | What it does |
|---|---|
| `GET /` | With `Accept: text/html` (i.e. a browser), this serves the dashboard (still admin-gated). Otherwise it returns an unauthenticated JSON index: `{name, version, links}`. |
| `GET /admin` | The dashboard itself, a static, client-rendered page (served from `app/static/`) that polls `/admin/status.json` for live data and posts to `/admin/cookies` when you import cookies. |
| `GET /admin/status.json` | The same full status payload as `GET /status` (live model list, quota/usage, warm-session counts, uptime included), always behind the admin credential regardless of how `status_access` is configured; this is what the dashboard itself polls. |
| `POST /admin/cookies` | Applies a fresh cookie export right now. Body: `{"cookies": "<any accepted format>"}`, or just a raw cookie string, or a cookie JSON array directly, same formats accepted everywhere else cookies are pasted in. Writes them to `cookie_file`, tears down and rebuilds the Gemini client, and returns `{applied, cookie_count, session_cookie_present, reinit_ok, cookie_mode}`. Returns `400` if the payload can't be parsed, `502` if the rebuild itself fails. |

**Keeping cookies fresh automatically:** set `cookie_watch_file` to a path,
and whatever's written there gets mirrored into `cookie_file` any time it
changes, a simple drop-a-file recovery mechanism for scripted or
automated cookie refresh. Separately, and automatically, whenever
`cookie_file`'s `__Secure-1PSID` value itself changes (meaning a genuinely
new session was pasted or dropped in), the client rebuilds on its own.
`__Secure-1PSIDTS` rotating on its own does **not** trigger a rebuild,
that's expected background rotation, not a new session. The poll interval
(and whether this watcher runs at all) is controlled by
`cookie_watch_interval`.

The JSON form of `GET /` stays open with no auth at all, by design, it only
exposes a name/version/links index, nothing account-specific. `GET /status`
is gated behind the admin credential by default (see
[`docs_access`/`status_access`](#docs-redoc-openapijson-and-status-are-configurable)
above); set `status_access: "open"` if you want it reachable without a
credential, e.g. for a monitoring tool that can't send one.

The dashboard's front end is plain static files in `app/static/`. If you
want your own look, you can replace those files entirely; the
`/admin/status.json` and `/admin/cookies` JSON contract they talk to is
stable, so a custom front end just needs to speak that same contract.

---

## Health and monitoring endpoints

`GET /healthz` needs no authentication at all, it's meant to be safe to hit
from an uptime checker or load balancer without handing out any credential.
`GET /status` is gated behind the admin credential by default (see
[`docs_access`/`status_access`](#docs-redoc-openapijson-and-status-are-configurable)),
configurable to `"open"` if your monitoring setup needs it credential-free.

### `GET /healthz`

A pure liveness check: "is the process up and responding at all." It never
touches Gemini, so it can't tell you anything about whether your session is
actually working, only that the server itself hasn't crashed.

```json
{ "status": "ok", "version": "0.1.0" }
```

### `GET /status`

The real health check. Unlike `/healthz`, this one attempts a lazy Gemini
client initialization, so the report actually reflects whether your
configured credentials currently work, not just whether the process is
alive. Gated behind the admin credential by default, same login as
`/admin`, see `status_access` if you want it open instead. `GET
/admin/status.json` returns this exact same payload and is what the
dashboard itself polls; the only difference is that it's always gated
regardless of how `status_access` is set.

```json
{
  "version": "0.1.0",
  "config_source": "/path/to/config.json",
  "health": {
    "overall": "ok",
    "page_reachable": true,
    "client_authenticated": true,
    "account_status": "AVAILABLE",
    "recent_requests_ok": true,
    "recent_window_hours": 1.0,
    "recent": { "total": 5, "ok": 5, "errors": 0, "error_rate": 0.0 }
  },
  "gemini": {
    "ready": true, "cookie_mode": "authenticated", "force_anonymous": false,
    "init_error": null, "cookie_file_present": true, "session_cookie_present": true,
    "cookie_cache_dir": "…/data/gemini_webapi", "access_token_present": true,
    "running": true, "cookie_source": "Base Cookies"
  },
  "activity": {
    "enabled": true, "window_hours": 24.0,
    "total": 42, "ok": 40, "errors": 2, "error_rate": 0.0476,
    "avg_latency_ms": 3120.5,
    "last_request_at": 1788248000.1, "seconds_since_last": 74.2,
    "per_model": { "gemini-flash": { "count": 30, "ok": 29 }, "gemini-pro": { "count": 12, "ok": 11 } },
    "errors_by_code": { "session_unauthenticated": 1, "upstream_timeout": 1 }
  }
}
```

#### `health`: three signals, checked separately on purpose

These three fail *independently* of each other, so they're reported
separately rather than collapsed into one boolean. The reason this matters:
a page can load fine while your actual session token has quietly failed,
if that got merged into one "healthy: true/false" flag, you'd have no way
to tell "the page loaded" apart from "this session is actually healthy",
and those are very different situations.

| Field | Meaning |
|---|---|
| `page_reachable` | a bare `GET` to `gemini.google.com/app` succeeded, meaning the cookie value at least isn't garbage (`null` if no client has been built yet) |
| `client_authenticated` | whether `gemini_webapi` considers this a real, authenticated account (`account_status == "AVAILABLE"`). This can be `false` even while `page_reachable` is `true`. |
| `account_status` | the underlying library's own status name: `AVAILABLE`, `UNAUTHENTICATED`, `ACCESS_TEMPORARILY_UNAVAILABLE`, etc. |
| `recent_requests_ok` | whether generations have actually been completing successfully over roughly the last hour (`null` if there haven't been any to judge) |
| `overall` | a convenience roll-up: `ok` / `degraded` / `down`. Useful for a quick glance, but check the individual signals above when something looks off, `overall` alone can't tell you *which* of the three is the problem. |

#### `gemini`: client and cookie detail

| Field | Meaning |
|---|---|
| `cookie_mode` | `authenticated` / `anonymous` / `anonymous(forced)` |
| `cookie_file_present` / `session_cookie_present` | whether the cookie file exists at all / whether it actually contains `__Secure-1PSID` |
| `cookie_source` | which credential group actually authenticated: `Cache`, `Base Cookies`, `Browser (...)`, or `Guest` |
| `cookie_cache_dir` | where `gemini_webapi` is keeping its own rotated-cookie cache |
| `init_error` | the last client-initialization failure message, if there was one |

#### `capacity`: the concurrency gate

`limit` (the configured `max_concurrent_generations`), `in_flight`
(generations running right now), `waiting` (requests currently queued for
a free slot), and `rejected_total` (how many `503`s this process has
returned so far). See [Concurrency and timeouts](#concurrency-and-timeouts)
above for the full picture.

#### `self_heal`: automatic degraded-session recovery

`enabled`, `interval`, `attempts`, `recoveries`, `last_attempt_at`,
`last_result` (`recovered` / `still degraded` / `error: …`). This task
exists because `gemini_webapi`'s own background refresh loop stops the
moment your account leaves the `AVAILABLE` state, so without this, a
degraded session would just stay degraded forever until someone manually
intervened. `self_heal` periodically re-attempts initialization while
`client_authenticated` is `false`, trying to recover on its own.

#### `activity`: your rolling 24h request history

A summary built from this service's own local request-history log. Every
generation attempt, on either wire format, gets recorded here off the main
request path (so a logging failure can never slow down or break an actual
request). `per_model` is keyed by the model that was *actually* served, not
what was requested. Token counts throughout this service are estimates, so
treat `avg_latency_ms` as the trustworthy number here, not the token
figures.

---

## Implementation status

Everything described in this document is implemented and covered by
automated tests. What's left is polish and operational maturity
(dashboard visual design, formal latency benchmarks for warm sessions),
not missing functionality.
