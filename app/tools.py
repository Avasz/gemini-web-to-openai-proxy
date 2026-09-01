"""Prompt-engineered tool / function calling (SRS 2.3, 2.4).

Gemini Web has no native function-calling protocol, so this is done entirely by:
  1. injecting a natural-language instruction block that describes the available
     tools and the exact plain-text syntax the model should emit to "call" one, and
  2. parsing that syntax back out of the reply and returning it in the wire
     format's structured shape, with the syntax stripped from the visible text.

Both OpenAI (`tools: [{type:"function", function:{...}}]`, `tool_choice`) and
Google (`tools: [{functionDeclarations:[...]}]`, `toolConfig.functionCallingConfig`)
request shapes are normalised to the same internal ``ToolSpec`` list.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

# The syntax we instruct the model to use. A fenced block keeps it clearly
# separable from prose.
_FENCE_TAG = "tool_call"


@dataclass
class ToolSpec:
    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolChoice:
    mode: str = "auto"  # "auto" | "required" | "none"
    allowed: list[str] = field(default_factory=list)  # restrict to these names


@dataclass
class ParsedToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")

    @property
    def arguments_json(self) -> str:
        return json.dumps(self.arguments, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Request normalisation
# --------------------------------------------------------------------------- #

def tools_from_openai(tools: Any) -> list[ToolSpec]:
    out: list[ToolSpec] = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") in (None, "function") else None
        fn = fn or (t if "name" in t else None)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append(
            ToolSpec(
                name=str(fn["name"]),
                description=str(fn.get("description", "")),
                parameters=fn.get("parameters") or fn.get("input_schema") or {},
            )
        )
    return out


def tools_from_google(tools: Any) -> list[ToolSpec]:
    out: list[ToolSpec] = []
    if isinstance(tools, dict):
        tools = [tools]
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        decls = t.get("functionDeclarations") or t.get("function_declarations") or []
        for d in decls:
            if isinstance(d, dict) and d.get("name"):
                out.append(
                    ToolSpec(
                        name=str(d["name"]),
                        description=str(d.get("description", "")),
                        parameters=d.get("parameters") or {},
                    )
                )
    return out


def choice_from_openai(tool_choice: Any) -> ToolChoice:
    if tool_choice in (None, "auto"):
        return ToolChoice("auto")
    if tool_choice == "none":
        return ToolChoice("none")
    if tool_choice in ("required", "any"):
        return ToolChoice("required")
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        name = fn.get("name") or tool_choice.get("name")
        if name:
            return ToolChoice("required", [str(name)])
        return ToolChoice("required")
    return ToolChoice("auto")


def choice_from_google(tool_config: Any) -> ToolChoice:
    if not isinstance(tool_config, dict):
        return ToolChoice("auto")
    fcc = (
        tool_config.get("functionCallingConfig")
        or tool_config.get("function_calling_config")
        or {}
    )
    mode = str(fcc.get("mode", "AUTO")).upper()
    allowed = [
        str(n)
        for n in (fcc.get("allowedFunctionNames") or fcc.get("allowed_function_names") or [])
    ]
    if mode == "NONE":
        return ToolChoice("none")
    if mode == "ANY":
        return ToolChoice("required", allowed)
    return ToolChoice("auto", allowed)


# --------------------------------------------------------------------------- #
# Prompt injection
# --------------------------------------------------------------------------- #

def build_tool_instructions(tools: list[ToolSpec], choice: ToolChoice) -> str:
    if not tools or choice.mode == "none":
        return ""

    visible = [
        t for t in tools if not choice.allowed or t.name in choice.allowed
    ] or tools

    lines: list[str] = [
        "## Tools available to you",
        "",
        "You can call the tools listed below instead of answering from your own "
        "knowledge. This is often the correct choice for anything time-sensitive, "
        "external, or that a tool is clearly designed for.",
        "",
        "To call a tool, your reply must contain a fenced code block tagged "
        f"`{_FENCE_TAG}` whose body is a single JSON object of the form "
        '`{"name": "<tool name>", "arguments": {<arguments matching the schema>}}`. '
        "Do not put anything else inside the block. You may include a short "
        "sentence of prose before the block. Example of the exact shape:",
        "",
        f"```{_FENCE_TAG}",
        '{"name": "example_tool", "arguments": {"some_arg": "some value"}}',
        "```",
        "",
        "After the tool result is sent back to you, continue normally.",
        "",
        "Tools:",
    ]
    for t in visible:
        params = json.dumps(t.parameters, ensure_ascii=False) if t.parameters else "{}"
        desc = f" — {t.description}" if t.description else ""
        lines.append(f"- `{t.name}`{desc}\n  arguments JSON Schema: {params}")

    if choice.mode == "required":
        if len(visible) == 1:
            lines.append(
                f"\nFor this turn you MUST call `{visible[0].name}`. Do not answer "
                f"from your own knowledge. Reply with the `{_FENCE_TAG}` block for "
                f"`{visible[0].name}` and nothing after it."
            )
        else:
            names = ", ".join(f"`{t.name}`" for t in visible)
            lines.append(
                f"\nFor this turn you MUST call one of: {names}. Do not answer from "
                f"your own knowledge. Reply with a single `{_FENCE_TAG}` block."
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Reply parsing
# --------------------------------------------------------------------------- #

# Tolerant on the language tag and -- critically (SRS 2.3) -- on there NOT being a
# newline directly before the closing fence.
_FENCE_RE = re.compile(
    r"```[ \t]*(?:tool_call|tool_code|toolcall|json|python)?[ \t]*\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
# Same, but the opening fence has no trailing newline before the payload.
_FENCE_INLINE_RE = re.compile(
    r"```[ \t]*(?:tool_call|tool_code|toolcall|json|python)[ \t]*(\{.*?\})[ \t]*```",
    re.DOTALL | re.IGNORECASE,
)


def _coerce_call(obj: Any) -> ParsedToolCall | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function") or obj.get("tool_name")
    if not name or not isinstance(name, str):
        return None
    args = (
        obj.get("arguments")
        if obj.get("arguments") is not None
        else obj.get("args", obj.get("parameters", obj.get("input", {})))
    )
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    return ParsedToolCall(name=name, arguments=args)


def _loads_lenient(payload: str) -> Any:
    payload = payload.strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        pass
    # trailing prose / multiple objects: take the first balanced {...}
    depth = 0
    start = -1
    for i, ch in enumerate(payload):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(payload[start : i + 1])
                except json.JSONDecodeError:
                    start = -1
    return None


def parse_tool_calls(text: str) -> tuple[list[ParsedToolCall], str]:
    """Return ``(tool_calls, visible_text)`` -- the tool-call syntax removed from
    the visible text (SRS 2.3). Falls back to a bare top-level JSON object with a
    name/args shape when no fenced block is present (SRS 2.4)."""
    if not text:
        return [], ""

    calls: list[ParsedToolCall] = []
    spans: list[tuple[int, int]] = []

    for rx in (_FENCE_RE, _FENCE_INLINE_RE):
        for m in rx.finditer(text):
            parsed = _loads_lenient(m.group(1))
            candidates = parsed if isinstance(parsed, list) else [parsed]
            got = [c for c in (_coerce_call(x) for x in candidates) if c]
            if got:
                calls.extend(got)
                spans.append((m.start(), m.end()))

    if calls:
        cleaned = _strip_spans(text, spans)
        return calls, cleaned

    # last-resort: the whole reply is (basically) a bare call object
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        obj = _loads_lenient(stripped)
        call = _coerce_call(obj)
        if call:
            return [call], ""

    return [], text


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text.strip()
    spans = sorted(set(spans))
    out = []
    prev = 0
    for a, b in spans:
        out.append(text[prev:a])
        prev = b
    out.append(text[prev:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
