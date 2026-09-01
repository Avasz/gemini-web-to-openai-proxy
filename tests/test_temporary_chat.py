"""SRS 2.10 -- temporary (unlogged) chat: global default + per-request override,
omitting the field falls back to the default (not a fixed value)."""


def test_default_false_no_override(client_factory, fake_client):
    with client_factory() as c:
        c.post("/v1/chat/completions",
               json={"model": "gemini-flash", "messages": [{"role": "user", "content": "x"}]})
    assert fake_client.calls[-1]["temporary"] is False


def test_config_default_true_no_override(client_factory, fake_client):
    with client_factory(temporary_chat_default=True) as c:
        c.post("/v1/chat/completions",
               json={"model": "gemini-flash", "messages": [{"role": "user", "content": "x"}]})
    assert fake_client.calls[-1]["temporary"] is True


def test_per_request_override_wins(client_factory, fake_client):
    with client_factory(temporary_chat_default=False) as c:
        c.post("/v1/chat/completions",
               json={"model": "gemini-flash", "temporary_chat": True,
                     "messages": [{"role": "user", "content": "x"}]})
    assert fake_client.calls[-1]["temporary"] is True

    with client_factory(temporary_chat_default=True) as c:
        c.post("/v1/chat/completions",
               json={"model": "gemini-flash", "temporary_chat": False,
                     "messages": [{"role": "user", "content": "x"}]})
    assert fake_client.calls[-1]["temporary"] is False


def test_google_temporarychat_field(client_factory, fake_client):
    with client_factory(temporary_chat_default=False) as c:
        c.post("/v1beta/models/gemini-flash:generateContent",
               json={"contents": [{"role": "user", "parts": [{"text": "x"}]}],
                     "temporaryChat": True})
    assert fake_client.calls[-1]["temporary"] is True


def test_responses_temporary_chat_field(client_factory, fake_client):
    with client_factory(temporary_chat_default=False) as c:
        c.post("/v1/responses",
               json={"model": "gemini-flash", "input": "x", "temporary_chat": True})
    assert fake_client.calls[-1]["temporary"] is True
