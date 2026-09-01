# Gemini Web → OpenAI-compatible API gateway

Exposes an authenticated `gemini.google.com` web session as an OpenAI-compatible and
Google-native HTTP API. Built to the spec in `SRS_CLEAN_ROOM_REWRITE.md`.

- **API reference:** [`docs/API.md`](docs/API.md) — every endpoint and parameter
- **Configuration reference:** [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) —
  config keys, cookie file formats, CLI flags, environment variables

## Status

Phase 1 (Foundation) complete:

- JSON config loading with discovery order and built-in defaults (`app/config.py`)
- `.env` loader for secrets (`app/dotenv.py`)
- Permissive cookie-export parsing + mtime-cached file store (`app/cookies.py`)
- One shared, lazily-initialized `gemini_webapi` client with anonymous/guest
  fallback and teardown/reinit (`app/gemini_service.py`)
- `GET /healthz` (liveness) and `GET /status` (Gemini client health) endpoints

Phase 2 (Core OpenAI-compatible chat) complete:

- `GET /v1/models` — live model list from the authenticated account
- `POST /v1/chat/completions` — non-streaming and SSE streaming (OpenAI delta format)
- Message array → single Gemini prompt, role sections preserved; image parts split
  out for Phase 4 (`app/translation.py`)
- Live model resolution with a `-high` reasoning suffix mapped to `gemini_webapi`'s
  on/off `extended_thinking`; unknown model → 400 listing the account's real models
  (`app/model_selection.py`)
- Every response carries `x_gemini_proxy` metadata: the validated served model
  (never the model's self-claim), model id, cookie mode, and live account usage
- Per-endpoint API-key auth, separate from the (future) admin credential (`app/auth.py`)
- Upstream failures (guest-tier model, expired session, usage cap, timeout) are
  classified into clean client errors with sensible status codes and logged at
  WARNING without a stack trace (`app/errors.py`); a non-default model on a guest
  session is rejected before the network call

Phase 3 (Google-native parity) complete:

- `GET /v1beta/models`, `GET /v1beta/models/{model}` — Google `{"models":[...]}` shape
- `POST /v1beta/models/{model}:generateContent` — `candidates` / `usageMetadata` /
  `modelVersion` response shape
- `POST /v1beta/models/{model}:streamGenerateContent` — JSON-array framing by
  default, SSE framing with `?alt=sse`
- `contents` + `systemInstruction` flattened to the same Gemini prompt; `inlineData`
  image parts split into the shared image list (`app/translation.py`)
- shares model resolution + generation plumbing with the OpenAI path
- `GEMINI_COOKIE_PATH` (the library's rotated-cookie cache) is pointed at
  `{data_dir}/gemini_webapi` so all local state is one mountable directory
- `/status` now reports the actual cookie source the client authenticated with
  (`Cache` / `Base Cookies` / `Browser (...)` / `Guest`)
- `force_anonymous` config option: ignore the cookie file *and* the library cache,
  disable auto-refresh — for verifying the credential-free path (SRS §7)

## Setup

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
```

## Run

```bash
python -m app                 # uses defaults / discovered config.json
python -m app --port 8000     # override
python -m app -c ./config.json --reload
```

Copy `config.example.json` to `config.json` to customize. With no config and no
cookies, the service starts in anonymous/guest tier.

Interactive API docs: `http://127.0.0.1:8000/docs` (provided by FastAPI).

## Tests

```bash
pytest                                    # unit tests, no network
python scripts/check_anonymous.py         # live: zero-credential guest session
python scripts/check_anonymous.py --prompt "say hi"
```

### Try the OpenAI API live

```bash
python -m app --port 8000

curl -s localhost:8000/v1/models | python -m json.tool

curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"gemini-flash","messages":[{"role":"user","content":"Reply with exactly: pong"}]}'

curl -sN localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"gemini-flash","stream":true,"messages":[{"role":"user","content":"Count 1 to 3"}]}'
```

Works with any OpenAI client by pointing `base_url` at `http://localhost:8000/v1`.
Append `-high` to a model name (e.g. `gemini-pro-high`) to enable extended thinking.

### Try the Google-native API live

```bash
curl -s localhost:8000/v1beta/models | python -m json.tool

curl -s "localhost:8000/v1beta/models/gemini-flash:generateContent" \
  -H 'content-type: application/json' \
  -d '{"contents":[{"role":"user","parts":[{"text":"Name 3 fruits"}]}]}'

curl -sN "localhost:8000/v1beta/models/gemini-flash:streamGenerateContent?alt=sse" \
  -H 'content-type: application/json' \
  -d '{"contents":[{"role":"user","parts":[{"text":"Count 1 to 3"}]}]}'
```

Point `google-genai` at it with `http_options={"base_url": "http://localhost:8000"}`
and `api_key` set to one of your configured `api_keys` (or anything if none).
