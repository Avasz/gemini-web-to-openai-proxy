import json

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def test_openai_tool_call_nonstreaming(client_factory, fake_client):
    fake_client.next_reply = (
        'Let me check.\n```tool_call\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n```'
    )
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-flash",
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [WEATHER_TOOL],
            },
        )
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tc = choice["message"]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "Paris"}
    assert "tool_call" not in (choice["message"]["content"] or "")
    # tool instructions were injected into the prompt
    assert "get_weather" in fake_client.calls[-1]["prompt"]


def test_openai_tool_call_no_newline_before_fence(client_factory, fake_client):
    fake_client.next_reply = '```tool_call\n{"name": "get_weather", "arguments": {"city": "NYC"}}```'
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-flash",
                "messages": [{"role": "user", "content": "x"}],
                "tools": [WEATHER_TOOL],
            },
        )
    tc = r.json()["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(tc["function"]["arguments"]) == {"city": "NYC"}


def test_openai_tool_choice_none_skips_injection(client_factory, fake_client):
    with client_factory() as c:
        c.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-flash",
                "messages": [{"role": "user", "content": "x"}],
                "tools": [WEATHER_TOOL],
                "tool_choice": "none",
            },
        )
    assert "get_weather" not in fake_client.calls[-1]["prompt"]


def test_openai_tool_call_streaming(client_factory, fake_client):
    fake_client.next_reply = '```tool_call\n{"name": "get_weather", "arguments": {"city": "Rome"}}\n```'
    with client_factory() as c:
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "gemini-flash",
                "stream": True,
                "messages": [{"role": "user", "content": "x"}],
                "tools": [WEATHER_TOOL],
            },
        ) as r:
            payloads = [
                ln[len("data: "):]
                for ln in r.iter_lines()
                if ln.startswith("data: ") and ln != "data: [DONE]"
            ]
    objs = [json.loads(p) for p in payloads]
    # no raw fence leaked as content
    assert all("```" not in (o["choices"][0]["delta"].get("content") or "") for o in objs)
    tc_frame = next(o for o in objs if o["choices"][0]["delta"].get("tool_calls"))
    tc = tc_frame["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert objs[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_google_function_call(client_factory, fake_client):
    fake_client.next_reply = '```tool_call\n{"name": "get_weather", "arguments": {"city": "Berlin"}}\n```'
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gemini-flash:generateContent",
            json={
                "contents": [{"role": "user", "parts": [{"text": "weather?"}]}],
                "tools": [
                    {"functionDeclarations": [{"name": "get_weather",
                                               "parameters": {"type": "object"}}]}
                ],
            },
        )
    assert r.status_code == 200
    parts = r.json()["candidates"][0]["content"]["parts"]
    fc = next(p["functionCall"] for p in parts if "functionCall" in p)
    assert fc["name"] == "get_weather"
    assert fc["args"] == {"city": "Berlin"}


def test_google_mode_any_forces_injection(client_factory, fake_client):
    with client_factory() as c:
        c.post(
            "/v1beta/models/gemini-flash:generateContent",
            json={
                "contents": [{"role": "user", "parts": [{"text": "x"}]}],
                "tools": [{"functionDeclarations": [{"name": "f"}]}],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            },
        )
    prompt = fake_client.calls[-1]["prompt"]
    assert "tool-call turn" in prompt.lower()


def test_tool_result_roundtrip_in_followup(client_factory, fake_client):
    fake_client.next_reply = "It is 20C in Paris."
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-flash",
                "tools": [WEATHER_TOOL],
                "messages": [
                    {"role": "user", "content": "weather in Paris?"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}
                    ]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "20C"},
                ],
            },
        )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "It is 20C in Paris."
    prompt = fake_client.calls[-1]["prompt"]
    assert "get_weather" in prompt and "20C" in prompt
