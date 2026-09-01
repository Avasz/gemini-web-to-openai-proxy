from app.translation import messages_to_prompt


def test_flatten_preserves_roles():
    b = messages_to_prompt(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "Bye"},
        ]
    )
    assert "System:\nBe terse." in b.prompt
    assert "User:\nHi" in b.prompt
    assert "Assistant:\nHello" in b.prompt
    assert b.prompt.strip().endswith("User:\nBye")
    assert b.images == []


def test_multimodal_parts_split_out():
    b = messages_to_prompt(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                ],
            }
        ]
    )
    assert "What is this?" in b.prompt
    assert "https://x/y.png" not in b.prompt
    assert [i.url for i in b.images] == ["https://x/y.png"]


def test_tool_messages_render_as_text():
    b = messages_to_prompt(
        [
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"city":"NYC"}'}}
            ]},
            {"role": "tool", "name": "get_weather", "content": "72F"},
        ]
    )
    assert "get_weather" in b.prompt
    assert "72F" in b.prompt
