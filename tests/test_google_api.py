import json


def test_list_models_google_shape(client_factory):
    with client_factory() as c:
        r = c.get("/v1beta/models")
        assert r.status_code == 200
        names = [m["name"] for m in r.json()["models"]]
        assert names == ["models/gemini-flash", "models/gemini-pro"]
        assert "generateContent" in r.json()["models"][0]["supportedGenerationMethods"]


def test_get_single_model(client_factory):
    with client_factory() as c:
        r = c.get("/v1beta/models/gemini-flash")
        assert r.status_code == 200
        assert r.json()["name"] == "models/gemini-flash"
        r2 = c.get("/v1beta/models/models/gemini-pro")
        assert r2.status_code == 200
        assert r2.json()["name"] == "models/gemini-pro"


def test_generate_content(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gemini-flash:generateContent",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "hello there"}]},
                ],
                "systemInstruction": {"parts": [{"text": "Be brief."}]},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["candidates"][0]["content"]["parts"][0]["text"].startswith("echo:")
        assert body["candidates"][0]["finishReason"] == "STOP"
        assert body["modelVersion"] == "gemini-flash"
        assert body["x_gemini_proxy"]["served_model"] == "gemini-flash"
    # system + user both made it into the prompt
    prompt = fake_client.calls[-1]["prompt"]
    assert "System:" in prompt and "Be brief." in prompt
    assert "User:" in prompt and "hello there" in prompt


def test_reasoning_suffix_google(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gemini-pro-high:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
        )
        assert r.status_code == 200
        assert r.json()["x_gemini_proxy"]["extended_thinking"] is True
        assert fake_client.calls[-1]["extended_thinking"] is True


def test_unknown_model_google(client_factory):
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gpt-4o:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
        )
        assert r.status_code == 400
        assert r.json()["error"]["status"] == "INVALID_ARGUMENT"
        assert "models/gemini-flash" in r.json()["error"]["availableModels"]


def test_stream_sse(client_factory):
    with client_factory() as c:
        with c.stream(
            "POST",
            "/v1beta/models/gemini-flash:streamGenerateContent?alt=sse",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        ) as r:
            assert r.status_code == 200
            payloads = [
                ln[len("data: "):]
                for ln in r.iter_lines()
                if ln.startswith("data: ")
            ]
    objs = [json.loads(p) for p in payloads]
    text = "".join(
        o["candidates"][0]["content"]["parts"][0]["text"]
        for o in objs
        if o["candidates"][0]["content"]["parts"]
    )
    assert text == "Hello world"
    assert objs[-1]["candidates"][0]["finishReason"] == "STOP"
    assert objs[-1]["x_gemini_proxy"]["served_model"] == "gemini-flash"


def test_stream_json_array(client_factory):
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gemini-flash:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )
        assert r.status_code == 200
        arr = r.json()
        assert isinstance(arr, list)
        text = "".join(
            o["candidates"][0]["content"]["parts"][0]["text"]
            for o in arr
            if o["candidates"][0]["content"]["parts"]
        )
        assert text == "Hello world"
        assert arr[-1]["candidates"][0]["finishReason"] == "STOP"


def test_inline_image_part_split_out(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gemini-flash:generateContent",
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "what is this"},
                            {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
                        ],
                    }
                ]
            },
        )
        assert r.status_code == 200
    assert "AAAA" not in fake_client.calls[-1]["prompt"]
