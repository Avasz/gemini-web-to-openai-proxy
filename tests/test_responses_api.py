import json


def _events(raw_lines):
    """Parse SSE 'event:'/'data:' line pairs into (name, obj) tuples."""
    out = []
    name = None
    for ln in raw_lines:
        if ln.startswith("event: "):
            name = ln[len("event: "):]
        elif ln.startswith("data: "):
            out.append((name, json.loads(ln[len("data: "):])))
    return out


def test_responses_string_input(client_factory, fake_client):
    with client_factory() as c:
        r = c.post("/v1/responses", json={"model": "gemini-flash", "input": "ping"})
    assert r.status_code == 200
    b = r.json()
    assert b["object"] == "response"
    assert b["status"] == "completed"
    assert b["model"] == "gemini-flash"
    assert b["output"][0]["type"] == "message"
    assert b["output"][0]["content"][0]["type"] == "output_text"
    assert b["output_text"].startswith("echo:")
    assert b["x_gemini_proxy"]["served_model"] == "gemini-flash"
    assert "User:\nping" in fake_client.calls[-1]["prompt"]


def test_responses_item_array_and_instructions(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1/responses",
            json={
                "model": "gemini-flash",
                "instructions": "Be terse.",
                "input": [
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello"}]},
                ],
            },
        )
    assert r.status_code == 200
    prompt = fake_client.calls[-1]["prompt"]
    assert "System:\nBe terse." in prompt
    assert "User:\nhello" in prompt


def test_responses_function_call_output_item(client_factory, fake_client):
    fake_client.next_reply = '```tool_call\n{"name": "get_time", "arguments": {"tz": "UTC"}}\n```'
    with client_factory() as c:
        r = c.post(
            "/v1/responses",
            json={
                "model": "gemini-flash",
                "input": "what time is it?",
                "tools": [{"type": "function", "name": "get_time",
                           "parameters": {"type": "object"}}],
            },
        )
    b = r.json()
    fc = next(it for it in b["output"] if it["type"] == "function_call")
    assert fc["name"] == "get_time"
    assert json.loads(fc["arguments"]) == {"tz": "UTC"}


def test_responses_prior_function_call_output_in_input(client_factory, fake_client):
    fake_client.next_reply = "It is noon UTC."
    with client_factory() as c:
        r = c.post(
            "/v1/responses",
            json={
                "model": "gemini-flash",
                "input": [
                    {"type": "message", "role": "user", "content": "time?"},
                    {"type": "function_call", "name": "get_time", "arguments": "{}",
                     "call_id": "call_x"},
                    {"type": "function_call_output", "call_id": "call_x",
                     "output": "12:00 UTC"},
                ],
            },
        )
    assert r.status_code == 200
    prompt = fake_client.calls[-1]["prompt"]
    assert "get_time" in prompt and "12:00 UTC" in prompt


def test_responses_streaming_events(client_factory, fake_client):
    with client_factory() as c:
        with c.stream(
            "POST", "/v1/responses",
            json={"model": "gemini-flash", "input": "hi", "stream": True},
        ) as r:
            assert r.status_code == 200
            evts = _events([ln for ln in r.iter_lines() if ln])
    names = [n for n, _ in evts]
    assert names[0] == "response.created"
    assert "response.output_text.delta" in names
    assert "response.output_text.done" in names
    assert names[-1] == "response.completed"
    text = "".join(
        o["delta"] for n, o in evts if n == "response.output_text.delta"
    )
    assert text == "Hello world"
    final = next(o for n, o in evts if n == "response.completed")
    assert final["response"]["output_text"] == "Hello world"
    assert final["response"]["status"] == "completed"


def test_responses_streaming_tool_call(client_factory, fake_client):
    fake_client.next_reply = '```tool_call\n{"name": "get_time", "arguments": {"tz": "UTC"}}\n```'
    with client_factory() as c:
        with c.stream(
            "POST", "/v1/responses",
            json={
                "model": "gemini-flash", "input": "time?", "stream": True,
                "tools": [{"type": "function", "name": "get_time"}],
            },
        ) as r:
            evts = _events([ln for ln in r.iter_lines() if ln])
    names = [n for n, _ in evts]
    assert "response.function_call_arguments.done" in names
    # no raw fence leaked through text deltas
    assert all(
        "```" not in o.get("delta", "")
        for n, o in evts
        if n == "response.output_text.delta"
    )
    fc_done = next(o for n, o in evts if n == "response.function_call_arguments.done")
    assert json.loads(fc_done["arguments"]) == {"tz": "UTC"}


def test_responses_unknown_model(client_factory):
    with client_factory() as c:
        r = c.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "model_not_found"


def test_responses_missing_input(client_factory):
    with client_factory() as c:
        r = c.post("/v1/responses", json={"model": "gemini-flash"})
    assert r.status_code == 400


def test_responses_is_independent_from_chat_completions(client_factory):
    """Both surfaces must work in the same app, not aliased."""
    with client_factory() as c:
        a = c.post("/v1/responses", json={"model": "gemini-flash", "input": "hi"})
        b = c.post("/v1/chat/completions",
                   json={"model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}]})
    assert a.json()["object"] == "response"
    assert b.json()["object"] == "chat.completion"
