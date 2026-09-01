# Deployment

## Local

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
cp config.example.json config.json      # edit to taste
# put a cookie export at ./cookies.json  (see docs/CONFIGURATION.md)
python -m app                            # or: uvicorn app.main:app --port 8000
```

Verify: `curl -s localhost:8000/status | python -m json.tool` — want
`health.overall: "ok"` and `client_authenticated: true`.

## Docker

The image runs `uvicorn app.main:app` on port 8000 and reads:

| Path | Contents | Mode |
|---|---|---|
| `/config/config.json` | your config (`GEMINI_PROXY_CONFIG` points here) | read |
| `/config/cookies.json` | your cookie export (`GEMINI_PROXY_COOKIE_FILE` points here) | read |
| `/data` | rotated-cookie cache, `activity.db`, `admin_credential` | **read/write, must persist** |

`data_dir` defaults to `/data` in the image. **Mount `/data` as a named volume** —
`gemini_webapi` rotates `__Secure-1PSIDTS` continuously and writes the fresh value
to `/data/gemini_webapi/`; if that is lost on a container recreate the service
falls back to the (now stale) cookie file and the session degrades (see the
`__Secure-1PSIDTS` trap in `docs/CONFIGURATION.md`).

### docker compose

```bash
mkdir -p deploy/config
cp config.example.json deploy/config/config.json
# add deploy/config/cookies.json
docker compose up -d --build
docker compose logs | grep -A4 "ADMIN DASHBOARD"   # the generated admin password
```

Pin the admin password instead of using the generated one by setting
`ADMIN_PASSWORD` in `docker-compose.yml` (or an `.env` file next to it).

### plain docker

```bash
docker build -t gemini-openai-proxy .
docker run -d --name gop -p 8000:8000 \
  -v "$PWD/deploy/config:/config" \
  -v gop-data:/data \
  -e ADMIN_PASSWORD=change-me \
  gemini-openai-proxy
```

## Configuration via environment

Any `GEMINI_PROXY_<KEY>` variable overrides the JSON config key `<key>`
(lower-cased), with type coercion — e.g. `GEMINI_PROXY_MAX_CONCURRENT_GENERATIONS=4`,
`GEMINI_PROXY_FORCE_ANONYMOUS=true`, `GEMINI_PROXY_API_KEYS=key1,key2`,
`GEMINI_PROXY_DEFAULT_MODEL=gemini-pro`. `GEMINI_PROXY_CONFIG` (config path) and
`GEMINI_PROXY_LOG_LEVEL` are reserved. Full key list: `docs/CONFIGURATION.md`.

## Operating notes

- **Don't restart-storm the process.** Each start does a cold `gemini_webapi`
  init; many cold re-auths in a short window degrade the Google account into an
  `UNAUTHENTICATED` state that takes hours to clear (SRS §7). A long-lived process
  keeps its session healthy via background refresh — leave it running.
- **One process per Google account.** Two instances refreshing the same account
  fight over `__Secure-1PSIDTS` rotation.
- **Recovering a dead session without a restart:** paste a fresh cookie export at
  `/admin` (or `POST /admin/cookies`), or drop it into `cookie_file` /
  `cookie_watch_file` — the client rebuilds automatically when `__Secure-1PSID`
  changes.
- **Health monitoring:** poll `GET /status` (no auth) or `GET /admin/status.json`
  (admin auth). Alert on `health.overall != "ok"`, `client_authenticated: false`,
  or a rising `activity.error_rate`.
- **Reverse proxy:** streaming endpoints need response buffering **off**
  (`proxy_buffering off;` in nginx) and a long read timeout — Gemini generations
  can take minutes (see the integrator note in `docs/API.md`).
