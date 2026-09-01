import json


def test_list_models(client_factory):
    with client_factory() as c:
        r = c.get("/v1/models")
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["data"]]
        assert ids == ["gemini-flash", "gemini-pro"]


def test_chat_completion_nonstreaming(client_factory):
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gemini-flash", "messages": [{"role": "user", "content": "ping"}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "gemini-flash"
        assert body["choices"][0]["message"]["content"].startswith("echo:")
        meta = body["x_gemini_proxy"]
        assert meta["served_model"] == "gemini-flash"
        assert meta["requested_model"] == "gemini-flash"
        assert meta["extended_thinking"] is False


def test_reasoning_suffix_sets_extended_thinking(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gemini-pro-high", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 200
        assert r.json()["x_gemini_proxy"]["served_model"] == "gemini-pro"
        assert r.json()["x_gemini_proxy"]["extended_thinking"] is True
        assert fake_client.calls[-1]["extended_thinking"] is True


def test_unknown_model_returns_400_with_list(client_factory):
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "model_not_found"
        assert "gemini-flash" in err["available_models"]


def test_default_model_used_when_omitted(client_factory):
    with client_factory(default_model="gemini-pro") as c:
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200
        assert r.json()["model"] == "gemini-pro"


def test_streaming(client_factory):
    with client_factory() as c:
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gemini-flash", "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        ) as r:
            assert r.status_code == 200
            lines = [ln for ln in r.iter_lines() if ln]
    payloads = [ln[len("data: "):] for ln in lines if ln.startswith("data: ")]
    assert payloads[-1] == "[DONE]"
    content = ""
    saw_stop = False
    for p in payloads[:-1]:
        obj = json.loads(p)
        delta = obj["choices"][0]["delta"]
        content += delta.get("content", "")
        if obj["choices"][0]["finish_reason"] == "stop":
            saw_stop = True
            assert obj["x_gemini_proxy"]["served_model"] == "gemini-flash"
    assert content == "Hello world"
    assert saw_stop


def test_api_key_enforced_when_configured(client_factory):
    with client_factory(api_keys=["secret"]) as c:
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 401
        r2 = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            json={"messages": [{"role": "user", "content": "x"}]},
        )
        assert r2.status_code == 200
