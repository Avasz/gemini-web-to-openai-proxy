from gemini_webapi.exceptions import GeminiError, UsageLimitExceededError

from app.errors import classify_upstream
from tests.conftest import FakeModel


def test_classify_unauthenticated():
    e = classify_upstream(
        GeminiError(
            "gemini-pro is not available for use. Account status: UNAUTHENTICATED - "
            "Session is not authenticated or cookies have expired."
        )
    )
    assert e.status_code == 403
    assert e.code == "session_unauthenticated"


def test_classify_usage_limit():
    e = classify_upstream(UsageLimitExceededError("usage limit exceeded"))
    assert e.status_code == 429
    assert e.code == "usage_limit_exceeded"


def test_classify_generic():
    e = classify_upstream(GeminiError("something weird happened"))
    assert e.status_code == 502


def test_unavailable_model_rejected_before_call(client_factory, fake_client):
    fake_client._models = [
        FakeModel("gemini-flash-lite", "x", ["flash-lite"], is_available=True),
        FakeModel("gemini-pro", "y", ["pro"], is_available=False),
    ]
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gemini-pro", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "model_unavailable"
        assert r.json()["error"]["available_models"] == ["gemini-flash-lite"]
    # generate_content was never invoked
    assert fake_client.calls == []


def test_upstream_geminierror_maps_to_clean_status(client_factory, fake_client):
    fake_client.raise_on_generate = GeminiError(
        "quota: usage limit exceeded for this account"
    )
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gemini-flash", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "usage_limit_exceeded"
