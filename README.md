# Gemini Web → OpenAI-compatible API gateway

An HTTP service that authenticates to `gemini.google.com` (the consumer Gemini web
app) with browser session cookies and exposes it as a programmable API in two
wire formats:

- **OpenAI-compatible** — `/v1/chat/completions`, `/v1/responses`, `/v1/models`
- **Google-native** — `/v1beta/models/{model}:generateContent` etc.

No Google Cloud project, no API key, no billing — it reuses one personal Google
account's existing Gemini access. Built clean-room to `SRS_CLEAN_ROOM_REWRITE.md`.

- **API reference:** [`docs/API.md`](docs/API.md)
- **Configuration:** [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)
- **Deployment:** [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- **Known gaps / follow-ups:** [`docs/BACKLOG.md`](docs/BACKLOG.md)

## Quick start

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
cp config.example.json config.json
```

Export your `google.com` cookies (Firefox recommended) to `./cookies.json` — a
JSON array, a `{"cookie": "..."}` object, or a raw `name=value; ...` string all
work. Verify before starting:

```bash
python scripts/check_cookies.py        # want: "OK - authenticated, all models available"
```

Then run and check:

```bash
python -m app                          # or: uvicorn app.main:app --port 8000
curl -s localhost:8000/status | python -m json.tool
```

Point any OpenAI client at `http://localhost:8000/v1` (`api_key` can be anything
unless you set `api_keys` in the config):

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
print(c.chat.completions.create(model="gemini-flash",
      messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)
```

## Features

| Area | |
|---|---|
| **Models** | live per-account list; `-high` suffix → extended thinking; unknown model → 4xx with the real list, never a silent swap |
| **OpenAI** | `/v1/chat/completions` + `/v1/responses` (independent surfaces), streaming, multimodal in/out, prompt-engineered tool calling |
| **Google-native** | `/v1beta` model list + `generateContent` + `streamGenerateContent` (`?alt=sse` or JSON array) |
| **Images** | URL / `data:` / `inlineData` in; MIME sniffed from magic bytes; generated images returned base64 inline |
| **Metadata** | every response carries `x_gemini_proxy` — the *validated* served model (not the model's self-claim), live quota |
| **Reliability** | one shared connection, capped at `max_concurrent_generations` (SRS 2.8); over the cap → `503`, retry |
| **Observability** | `/status` — three independent health signals + a 24h request-history summary (`data_dir/activity.db`) |
| **Admin** | dashboard at `/` (or `/admin`) + hot cookie reload, own credential, separate from `api_keys`; front end is static (`app/static/`) — swap it, the JSON contract stays |
| **Temporary chat** | per-request / config default; keeps the chat out of Google's account history |
| **Warm sessions** | opt-in `/v1/sessions` — **experimental**, see `docs/BACKLOG.md` |

## Operating notes

- **Don't restart-storm the process.** Each start does a cold Gemini auth; many
  cold re-auths in a short window push the Google account into an
  `UNAUTHENTICATED` state that takes hours to clear (SRS §7). Leave it running —
  the background refresh keeps the session alive.
- **One process per Google account** — two refreshers fight over cookie rotation.
- A cookie export's `__Secure-1PSIDTS` goes stale within ~30 min; export it
  fresh, or let the running process's auto-refresh take over. See the
  `__Secure-1PSIDTS` trap in `docs/CONFIGURATION.md`.
- Gemini generations can stall for minutes; "succeeded server-side" ≠ "client
  still connected". Use generous client timeouts.

## Tests

```bash
pytest                                 # ~120 tests, fully offline (fake gemini client)
python scripts/check_anonymous.py      # live: zero-credential guest session
python scripts/check_cookies.py        # live: verify cookies.json per-model
```
