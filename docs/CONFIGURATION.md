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

A `GEMINI_PROXY_<KEY>` environment variable overrides the matching config key
(see [Environment variables](#environment-variables)) — useful for containers.

Relative paths in the config (`cookie_file`, `data_dir`) resolve against the
**config file's own directory**; if there is no config file they resolve against
the current working directory.

---

## Config keys

| Key | Type | Default | Status | Meaning |
|---|---|---|---|---|
| `host` | string | `"127.0.0.1"` | active | listen address (overridden by `--host`) |
| `port` | integer | `8000` | active | listen port (overridden by `--port`) |
| `request_timeout` | number (s) | `180.0` | active | hard ceiling on a single **non-streaming** generation (outer `asyncio` timeout → `504`) |
| `default_model` | string | `"gemini-flash"` | active | model used when a request omits `model` |
| `api_keys` | string[] | `[]` | active | generation-endpoint keys; empty ⇒ endpoints open (startup warning) |
| `cookie_file` | string (path) | `"cookies.json"` | active | Gemini Web session cookie export (see below) |
| `temporary_chat_default` | boolean | `false` | active | default for "don't save to Gemini account history"; per-request override exists |
| `force_anonymous` | boolean | `false` | active | ignore the cookie file **and** the library's rotated-cookie cache, disable auto-refresh — forces guest tier (SRS §7) |
| `connection_timeout` | number (s) | `60.0` | active | passed to `gemini_webapi` as its request timeout |
| `zombie_stream_timeout` | number (s) | `90.0` | active | passed as the library's stream watchdog timeout |
| `cookie_refresh_interval` | number (s) | `600.0` | active | how often the library refreshes cookies/token in the background |
| `cookie_cache_dir` | string (path) or null | `null` | active | where `gemini_webapi` keeps its rotated-cookie cache. `null` ⇒ a pre-set `$GEMINI_COOKIE_PATH`, else `{data_dir}/gemini_webapi` |
| `auto_refresh` | boolean | `true` | active | let the library keep the session token fresh in the background. Turn **off** only when another process already owns refresh for the same account |
| `max_concurrent_generations` | integer | `3` | active | how many generations may run against the one shared upstream connection at once (SRS 2.8). `2`–`4` recommended; `1` serialises all callers (a warning is logged) |
| `slot_wait_timeout` | number (s) | `60.0` | active | how long a request waits for a generation slot before a `503` (`code: "capacity"`) |
| `activity_log_retention_days` | integer | `7` | active | how long request-history rows are kept in `{data_dir}/activity.db` |
| `warm_session_idle_timeout` | number (s) | `900.0` | active | a warm session is dropped after this long without use |
| `max_warm_sessions` | integer | `20` | active | cap on live warm sessions; the least-recently-used is evicted past this |
| `admin_username` | string | `"admin"` | active | username for the admin dashboard's HTTP Basic auth |
| `cookie_watch_interval` | number (s) | `15.0` | active | how often to check the cookie file for a new session; `0` disables the watcher |
| `cookie_watch_file` | string (path) or null | `null` | active | an extra file mirrored into `cookie_file` when it changes (drop-a-file recovery) |
| `image_fetch_timeout` | number (s) | `20.0` | active | per-image timeout when fetching a remote image URL for input |
| `max_image_bytes` | integer | `20971520` (20 MiB) | active | max decoded size of a single input image; larger is rejected (that image only) |
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

Verify an export before trusting it:

```bash
python scripts/check_cookies.py path/to/cookies.json
```

It prints the `__Secure-1PSID` fingerprint (so two exports can be told apart), the
resolved account status, and per-model availability. A real authenticated session
shows the non-default models as usable; a **GUEST / UNAUTHENTICATED** verdict
means the export is the wrong account, an unprovisioned account (open Gemini in
that browser and send one message first), or a device-bound Chromium export.

Note: `__Secure-1PSID` belongs to the browser's **primary** Google account —
switching accounts inside the Gemini web UI does not change it. Export from a
browser/profile signed into **only** the account you want.

The file is re-read automatically when its modification time or size changes — a
fresh export is picked up on the next request, **but the already-initialized
Gemini client is not rebuilt** until the process restarts (a hot reload endpoint
arrives in Phase 8). A file that
can't be parsed is logged and treated as "no cookies" rather than crashing.

> `gemini_webapi` also keeps its own rotated-cookie cache under
> `{data_dir}/gemini_webapi/` and (once the first auth succeeds) keeps it alive
> indefinitely via background refresh. Deleting `cookie_file` alone does **not**
> end the session — clear that directory too, or set `force_anonymous: true`.

### Temporary (unlogged) chat

`temporary_chat_default` (and the per-request override — `temporary_chat` on the
OpenAI/Responses bodies, `temporaryChat` on Google) sets Gemini's own
"temporary chat" flag, which keeps the conversation **out of the Google
account's saved chat history**.

It has **no effect on this service's own request history** (`activity.db`,
`/status.activity`) — that always records that a request happened (model,
latency, ok/fail; never the prompt text). If you need true end-to-end
non-logging, set `activity_log_retention_days` low / disable the DB *and* use
temporary chat.

---

### The `__Secure-1PSIDTS` freshness trap

`__Secure-1PSIDTS` rotates every ~10–30 minutes and Google tracks the current
value server-side. If you present a **stale** one, Gemini still hands back a token
but marks the session `UNAUTHENTICATED` (`/status` shows `authenticated` from the
cookie file, but non-default models and image upload are refused and
`account_status` is not `AVAILABLE`).

A cookie export captured minutes ago is fine — the library's background refresh
then keeps it current. An export that has been sitting in `cookie_file` for an
hour is likely stale on a cold start. If you see the symptoms above with cookies
you know are for the right, provisioned account:

- re-export **right after** loading `gemini.google.com` in the browser, and start
  the service promptly, **or**
- point `cookie_cache_dir` at a directory that another *running* instance of the
  same account keeps fresh, and set `auto_refresh: false` here so the two don't
  both rotate the token (two refreshers invalidate each other).

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
| `GEMINI_COOKIE_PATH` | where `gemini_webapi` stores its cookie cache. Honored if set before startup (else `{data_dir}/gemini_webapi`); `cookie_cache_dir` in the config wins over it |
| `GEMINI_PROXY_<KEY>` | overrides config key `<key>` (lower-cased) with type coercion — e.g. `GEMINI_PROXY_PORT=9000`, `GEMINI_PROXY_FORCE_ANONYMOUS=true`, `GEMINI_PROXY_API_KEYS=k1,k2`. `GEMINI_PROXY_CONFIG` and `GEMINI_PROXY_LOG_LEVEL` are reserved (above) |

### `.env` file

A minimal `.env` loader runs at startup: `KEY=VALUE` lines, `#` comments, quotes
stripped from values. A variable already present in the real environment always
wins.

| Variable | Effect |
|---|---|
| `ADMIN_PASSWORD` | pins the admin-dashboard password. If unset, a random one is generated on first boot, written to `{data_dir}/admin_credential` (mode 600), and logged prominently at startup. |

---

## `data_dir` contents

| Path | Written by | Purpose |
|---|---|---|
| `{data_dir}/gemini_webapi/` | `gemini_webapi` | rotated `__Secure-1PSIDTS` cache, kept fresh by background refresh |
| `{data_dir}/gemini_webapi_anon/` | `gemini_webapi` | same, but only used when `force_anonymous: true` (never sees an authenticated session) |
| `{data_dir}/activity.db` | this service | SQLite request history (per-request model / latency / ok / error code), pruned to `activity_log_retention_days` |
| `{data_dir}/admin_credential` | this service | the generated admin-dashboard password (mode 600); not created if `ADMIN_PASSWORD` is set |

Future phases add the request-history database and the admin-credential file here.
For Docker, mount `data_dir` as a persistent volume so a container recreate
doesn't discard cookies the library already rotated past.
