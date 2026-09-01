import base64

from tests.conftest import PNG_1PX, FakeImage

DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()


def test_openai_input_image_reaches_generate(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {"type": "image_url", "image_url": {"url": DATA_URL}},
                        ],
                    }
                ],
            },
        )
        assert r.status_code == 200
    files = fake_client.calls[-1]["files"]
    assert files and str(files[0]).endswith(".png")


def test_openai_output_image_in_response(client_factory, fake_client):
    fake_client.next_images = [FakeImage()]
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "gemini-flash", "messages": [{"role": "user", "content": "draw a cat"}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["images"][0]["mime_type"] == "image/png"
        assert base64.b64decode(body["images"][0]["data"]) == PNG_1PX
        assert body["choices"][0]["message"]["images"][0]["data"]
        assert body["x_gemini_proxy"]["output_image_count"] == 1


def test_openai_streaming_output_image_final_chunk(client_factory, fake_client):
    fake_client.next_images = [FakeImage()]
    with client_factory() as c:
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gemini-flash", "stream": True,
                  "messages": [{"role": "user", "content": "draw"}]},
        ) as r:
            lines = [ln for ln in r.iter_lines() if ln.startswith("data: ") and ln != "data: [DONE]"]
    import json

    last = json.loads(lines[-1][len("data: "):])
    assert last["images"][0]["mime_type"] == "image/png"


def test_google_input_inlinedata_reaches_generate(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gemini-flash:generateContent",
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "describe"},
                            {"inlineData": {"mimeType": "image/png",
                                            "data": base64.b64encode(PNG_1PX).decode()}},
                        ],
                    }
                ]
            },
        )
        assert r.status_code == 200
    files = fake_client.calls[-1]["files"]
    assert files and str(files[0]).endswith(".png")


def test_google_output_image_as_inlinedata_part(client_factory, fake_client):
    fake_client.next_images = [FakeImage()]
    with client_factory() as c:
        r = c.post(
            "/v1beta/models/gemini-flash:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "draw"}]}]},
        )
        assert r.status_code == 200
        parts = r.json()["candidates"][0]["content"]["parts"]
        inline = [p for p in parts if "inlineData" in p]
        assert inline and inline[0]["inlineData"]["mimeType"] == "image/png"
        assert r.json()["images"][0]["data"]


def test_bad_input_image_reported_not_fatal(client_factory, fake_client):
    with client_factory() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hi"},
                            {"type": "image_url",
                             "image_url": {"url": "data:image/png;base64,@@@bad@@@"}},
                        ],
                    }
                ],
            },
        )
        assert r.status_code == 200
        assert r.json()["x_gemini_proxy"]["input_image_errors"]
    assert fake_client.calls[-1]["files"] is None
