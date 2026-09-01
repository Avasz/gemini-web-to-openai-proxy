import base64


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _cred(c):
    return c.app.state.admin_credential


def test_docs_gated_behind_admin_by_default(client_factory):
    with client_factory() as c:
        assert c.get("/docs").status_code == 401
        assert c.get("/redoc").status_code == 401
        assert c.get("/openapi.json").status_code == 401


def test_docs_reachable_with_admin_credential(client_factory):
    with client_factory() as c:
        pw = _cred(c).password
        assert c.get("/docs", headers=_basic("admin", pw)).status_code == 200
        assert c.get("/redoc", headers=_basic("admin", pw)).status_code == 200
        r = c.get("/openapi.json", headers=_basic("admin", pw))
        assert r.status_code == 200
        assert "paths" in r.json()


def test_docs_access_open(client_factory):
    with client_factory(docs_access="open") as c:
        assert c.get("/docs").status_code == 200
        assert c.get("/openapi.json").status_code == 200


def test_docs_access_disabled(client_factory):
    with client_factory(docs_access="disabled") as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/redoc").status_code == 404
        assert c.get("/openapi.json").status_code == 404


def test_status_access_disabled(client_factory):
    with client_factory(status_access="disabled") as c:
        assert c.get("/status").status_code == 404
