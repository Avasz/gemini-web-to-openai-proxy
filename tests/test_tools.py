import pytest

from app.tools import (
    ToolChoice,
    ToolSpec,
    build_tool_instructions,
    choice_from_google,
    choice_from_openai,
    parse_tool_calls,
    tools_from_google,
    tools_from_openai,
)

WEATHER = ToolSpec("get_weather", "Get weather", {"type": "object", "properties": {"city": {"type": "string"}}})


def test_tools_from_openai():
    specs = tools_from_openai(
        [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"x": 1}}}]
    )
    assert specs[0].name == "f" and specs[0].parameters == {"x": 1}


def test_tools_from_google():
    specs = tools_from_google(
        [{"functionDeclarations": [{"name": "f", "description": "d", "parameters": {"x": 1}}]}]
    )
    assert specs[0].name == "f"


def test_choice_mapping():
    assert choice_from_openai("none").mode == "none"
    assert choice_from_openai("required").mode == "required"
    assert choice_from_openai({"type": "function", "function": {"name": "f"}}).allowed == ["f"]
    assert choice_from_google({"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["f"]}}).allowed == ["f"]
    assert choice_from_google({"functionCallingConfig": {"mode": "NONE"}}).mode == "none"


def test_instructions_include_tool_and_required_note():
    block = build_tool_instructions([WEATHER], ToolChoice("required", ["get_weather"]))
    assert "get_weather" in block
    assert "MUST call" in block
    block2 = build_tool_instructions([WEATHER], ToolChoice("none"))
    assert block2 == ""


def test_parse_standard_fenced_call():
    calls, visible = parse_tool_calls(
        'Sure.\n```tool_call\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n```\n'
    )
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Paris"}
    assert "get_weather" not in visible
    assert visible.strip() == "Sure."


def test_parse_no_newline_before_closing_fence():
    # SRS 2.3: the model often omits the newline before the closing ```
    text = '```tool_call\n{"name": "get_weather", "arguments": {"city": "NYC"}}```'
    calls, visible = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].arguments == {"city": "NYC"}
    assert visible == ""


def test_parse_json_tag_and_multiple_calls():
    text = (
        '```json\n{"name": "a", "arguments": {}}\n```\n'
        'and\n```tool_call\n{"name": "b", "arguments": {"n": 2}}```'
    )
    calls, _ = parse_tool_calls(text)
    assert [c.name for c in calls] == ["a", "b"]


def test_parse_bare_json_object_last_resort():
    calls, visible = parse_tool_calls('{"name": "search", "args": {"q": "cats"}}')
    assert calls[0].name == "search"
    assert calls[0].arguments == {"q": "cats"}
    assert visible == ""


def test_parse_no_call_returns_text_untouched():
    calls, visible = parse_tool_calls("Just a normal answer.")
    assert calls == []
    assert visible == "Just a normal answer."


def test_arguments_json_is_a_string():
    calls, _ = parse_tool_calls('```tool_call\n{"name":"x","arguments":{"a":1}}\n```')
    assert calls[0].arguments_json == '{"a": 1}'
