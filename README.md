# Gemini Web → OpenAI-compatible API gateway

Exposes an authenticated `gemini.google.com` web session as an OpenAI-compatible and
Google-native HTTP API. Built to the spec in `SRS_CLEAN_ROOM_REWRITE.md`.

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
