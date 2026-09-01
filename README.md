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
