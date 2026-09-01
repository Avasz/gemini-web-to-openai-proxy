# Configuration Reference

Covers everything implemented through **Phase 3**: the JSON config file, the
cookie file, CLI flags, and environment variables.

See also [`docs/API.md`](API.md) for the HTTP endpoints.

---

## Config file discovery

On startup the first file found in this order is loaded (the rest are ignored):

1. path passed with `-c` / `--config`
2. path named by the `GEMINI_PROXY_CONFIG` environment variable
3. `./config.json` (current working directory)
4. `$XDG_CONFIG_HOME/gemini-openai-proxy/config.json`
   (falls back to `~/.config/gemini-openai-proxy/config.json`)

If none exist, **built-in defaults are used** — the service runs with zero
configuration (anonymous/guest tier, open endpoints).

The file must be a single JSON object. Unknown keys are ignored. Any key you omit
falls back to its default. Copy [`config.example.json`](../config.example.json)
to start.

Relative paths in the config (`cookie_file`, `data_dir`) resolve against the
**config file's own directory**; if there is no config file they resolve against
the current working directory.

---

## Config keys

| Key | Type | Default | Status | Meaning |
|---|---|---|---|---|
| `host` | string | `"127.0.0.1"` | active | listen address (overridden by `--host`) |
| `port` | integer | `8000` | active | listen port (overridden by `--port`) |
| `request_timeout` | number (s) | `120.0` | reserved | outer request budget; not yet enforced |
| `default_model` | string | `"gemini-flash"` | active | model used when a request omits `model` |
| `api_keys` | string[] | `[]` | active | generation-endpoint keys; empty ⇒ endpoints open (startup warning) |
| `cookie_file` | string (path) | `"cookies.json"` | active | Gemini Web session cookie export (see below) |
| `temporary_chat_default` | boolean | `false` | active | default for "don't save to Gemini account history"; per-request override exists |
| `force_anonymous` | boolean | `false` | active | ignore the cookie file **and** the library's rotated-cookie cache, disable auto-refresh — forces guest tier (SRS §7) |
| `connection_timeout` | number (s) | `60.0` | active | passed to `gemini_webapi` as its request timeout |
| `zombie_stream_timeout` | number (s) | `90.0` | active | passed as the library's stream watchdog timeout |
| `cookie_refresh_interval` | number (s) | `600.0` | active | how often the library refreshes cookies/token in the background |
| `max_concurrent_generations` | integer | `3` | reserved | in-flight generation cap (Phase 9) |
| `activity_log_retention_days` | integer | `7` | reserved | local request-history retention (Phase 7) |
| `warm_session_idle_timeout` | number (s) | `900.0` | reserved | warm-session pruning (Phase 10) |
| `data_dir` | string (path) | `"data"` | active | directory for local state; currently holds the library's cookie cache (`{data_dir}/gemini_webapi`, or `{data_dir}/gemini_webapi_anon` when `force_anonymous`) |

**Status:** *active* = read and applied today; *reserved* = accepted and
validated but the feature that uses it lands in a later phase.

### Example

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "default_model": "gemini-flash",
  "api_keys": ["sk-local-abc123"],
  "cookie_file": "cookies.json",
  "temporary_chat_default": true,
  "connection_timeout": 60.0,
  "data_dir": "data"
}
```

---

## Cookie file

Path is `cookie_file` in the config. **A missing or empty file is not an error** —
the service falls back to Gemini's anonymous/guest tier (only the default model is
usable there).

The two cookies that carry a session are `__Secure-1PSID` (long-lived) and
`__Secure-1PSIDTS` (short-lived, auto-rotated). Export the **whole `google.com`
cookie set** if you can — the refresh path touches `accounts.google.com`.

Recommended: export from **Firefox**. Chromium-based browsers (Chrome, Edge,
Brave) can bind an exported session to the device so it dies within hours.

### Accepted formats (auto-detected — you don't declare which)

**1. Browser-extension JSON array** (e.g. "Cookie-Editor" export):

```json
[
  { "name": "__Secure-1PSID",   "value": "g.a000…", "domain": ".google.com" },
  { "name": "__Secure-1PSIDTS", "value": "sidts-…", "domain": ".google.com" }
]
```

**2. Wrapped raw string:**

```json
{ "cookie": "__Secure-1PSID=g.a000…; __Secure-1PSIDTS=sidts-…; NID=…" }
```

**3. Raw cookie-header string** (file contents, no JSON):

```
__Secure-1PSID=g.a000…; __Secure-1PSIDTS=sidts-…; NID=…
```

**4. Flat JSON object:** `{ "__Secure-1PSID": "…", "__Secure-1PSIDTS": "…" }`

The file is re-read automatically when its modification time or size changes — a
fresh export is picked up on the next request, no restart needed. A file that
can't be parsed is logged and treated as "no cookies" rather than crashing.

> `gemini_webapi` also keeps its own rotated-cookie cache under
> `{data_dir}/gemini_webapi/` and (once the first auth succeeds) keeps it alive
> indefinitely via background refresh. Deleting `cookie_file` alone does **not**
> end the session — clear that directory too, or set `force_anonymous: true`.

---

## CLI flags

```
python -m app [options]
```

| Flag | Meaning |
|---|---|
| `-c PATH`, `--config PATH` | explicit config file path (discovery step 1) |
| `--host HOST` | override `host` |
| `--port PORT` | override `port` |
| `--reload` | uvicorn dev auto-reload (re-imports `app.main:app`) |

You can also run it as a plain ASGI app, bypassing the CLI wrapper (host/port then
come only from the config file or uvicorn's own flags):

```bash
uvicorn app.main:app --port 8000
```

---

## Environment variables

| Variable | Effect |
|---|---|
| `GEMINI_PROXY_CONFIG` | config file path (discovery step 2) |
| `GEMINI_PROXY_LOG_LEVEL` | log level for the app logger (default `INFO`) |
| `XDG_CONFIG_HOME` | base for the user-level config lookup (discovery step 4) |
| `GEMINI_COOKIE_PATH` | where `gemini_webapi` stores its cookie cache. **The service sets this itself** to `{data_dir}/gemini_webapi[_anon]` on startup; setting it yourself is overridden |

### `.env` file

A minimal `.env` loader runs at startup: `KEY=VALUE` lines, `#` comments, quotes
stripped from values. A variable already present in the real environment always
wins. This exists for secrets that shouldn't sit in the JSON config — it will
carry the admin credential in Phase 8. It has no required keys today.

---

## `data_dir` contents

| Path | Written by | Purpose |
|---|---|---|
| `{data_dir}/gemini_webapi/` | `gemini_webapi` | rotated `__Secure-1PSIDTS` cache, kept fresh by background refresh |
| `{data_dir}/gemini_webapi_anon/` | `gemini_webapi` | same, but only used when `force_anonymous: true` (never sees an authenticated session) |

Future phases add the request-history database and the admin-credential file here.
For Docker, mount `data_dir` as a persistent volume so a container recreate
doesn't discard cookies the library already rotated past.
