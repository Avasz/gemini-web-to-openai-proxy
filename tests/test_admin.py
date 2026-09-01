import base64
import json

import pytest

from app.admin_auth import resolve_admin_credential


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


# ---- credential resolution -------------------------------------------------- #

def test_credential_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    cred = resolve_admin_credential(tmp_path, "admin")
    assert cred.password == "s3cret"
    assert cred.source == "env"
    assert not (tmp_path / "admin_credential").exists()


def test_credential_generated_and_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    cred = resolve_admin_credential(tmp_path, "admin")
    assert cred.source == "generated"
    f = tmp_path / "admin_credential"
    assert f.read_text().strip() == cred.password
    assert oct(f.stat().st_mode)[-3:] == "600"
    # second call loads the same one
    assert resolve_admin_credential(tmp_path, "admin").password == cred.password


# ---- dashboard auth ------------------------------------------------------- #

def _cred(c):
    return c.app.state.admin_credential


def test_admin_page_requires_auth(client_factory):
    with client_factory() as c:
        r = c.get("/admin")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].lower().startswith("basic")


def test_admin_page_basic_auth(client_factory):
    with client_factory() as c:
        pw = _cred(c).password
        r = c.get("/admin", headers=_basic("admin", pw))
        assert r.status_code == 200
        assert "gemini-openai-proxy" in r.text
        assert "import cookies" in r.text.lower()


def test_admin_page_header_and_query(client_factory):
    with client_factory() as c:
        pw = _cred(c).password
        assert c.get("/admin", headers={"X-Admin-Password": pw}).status_code == 200
        assert c.get(f"/admin?admin_key={pw}").status_code == 200
        assert c.get("/admin?admin_key=wrong").status_code == 401


def test_generation_api_key_does_not_grant_admin(client_factory):
    with client_factory(api_keys=["genkey"]) as c:
        r = c.get("/admin", headers={"Authorization": "Bearer genkey"})
        assert r.status_code == 401


# ---- cookie import ------------------------------------------------------- #

def test_admin_apply_cookies(client_factory, fake_client, tmp_path):
    with client_factory() as c:
        pw = _cred(c).password
        payload = json.dumps([
            {"name": "__Secure-1PSID", "value": "g.aNEW"},
            {"name": "__Secure-1PSIDTS", "value": "sidts-x"},
        ])
        r = c.post(
            "/admin/cookies",
            headers={"X-Admin-Password": pw, "content-type": "application/json"},
            json={"cookies": payload},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True
        assert body["cookie_count"] == 2
        assert body["session_cookie_present"] is True
        assert body["reinit_ok"] is True
        # written to the configured cookie file
        written = json.loads((tmp_path / "cookies.json").read_text())
        assert any(x["name"] == "__Secure-1PSID" and x["value"] == "g.aNEW" for x in written)


def test_admin_apply_cookies_rejects_garbage(client_factory):
    with client_factory() as c:
        pw = _cred(c).password
        r = c.post(
            "/admin/cookies",
            headers={"X-Admin-Password": pw, "content-type": "application/json"},
            json={"cookies": "not a cookie at all"},
        )
        assert r.status_code == 400


def test_admin_apply_cookies_requires_auth(client_factory):
    with client_factory() as c:
        r = c.post("/admin/cookies", json={"cookies": "a=1"})
        assert r.status_code == 401


def test_admin_status_json(client_factory):
    with client_factory() as c:
        pw = _cred(c).password
        r = c.get("/admin/status.json", headers={"X-Admin-Password": pw})
        assert r.status_code == 200
        assert "health" in r.json() and "activity" in r.json()
        assert c.get("/admin/status.json").status_code == 401
