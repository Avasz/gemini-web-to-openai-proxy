"""Translate an OpenAI ``messages`` array into the single text prompt that
Gemini Web takes (SRS 2.3).

Gemini Web has no notion of roles or a structured message list, so we flatten the
conversation into one prompt string with clearly delimited per-role sections, and
collect any referenced images into a separate list (passed alongside the prompt,
never embedded in the text). Image *fetching/decoding* lands in Phase 4; this
module already separates image parts out so the wiring is in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool result",
    "function": "Tool result",
    "developer": "System",
}


@dataclass
class ImageRef:
    """A referenced image, not yet fetched. ``url`` is either a remote URL or a
    ``data:`` URL."""

    url: str
    detail: str | None = None


@dataclass
class PromptBundle:
    prompt: str
    images: list[ImageRef] = field(default_factory=list)
    had_unsupported_parts: bool = False


def _text_from_content(content: Any, images: list[ImageRef]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                chunks.append(str(part))
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text", "output_text") or "text" in part:
                chunks.append(str(part.get("text", "")))
            elif ptype in ("image_url", "input_image"):
                img = part.get("image_url")
                url = img.get("url") if isinstance(img, dict) else img
                url = url or part.get("image_url") or part.get("url")
                if url:
                    detail = img.get("detail") if isinstance(img, dict) else None
                    images.append(ImageRef(url=str(url), detail=detail))
        return "\n".join(c for c in chunks if c)
    return str(content)


_GOOGLE_ROLE_LABELS = {
    "user": "User",
    "model": "Assistant",
    "function": "Tool result",
    "tool": "Tool result",
}


def _google_parts_text(parts: Any, images: list[ImageRef]) -> str:
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            chunks.append(str(part))
            continue
        if "text" in part and part["text"] is not None:
            chunks.append(str(part["text"]))
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            data = inline.get("data", "")
            if data:
                images.append(ImageRef(url=f"data:{mime};base64,{data}"))
        file_data = part.get("fileData") or part.get("file_data")
        if isinstance(file_data, dict) and file_data.get("fileUri"):
            images.append(ImageRef(url=str(file_data["fileUri"])))
        fc = part.get("functionCall") or part.get("function_call")
        if isinstance(fc, dict):
            chunks.append(
                f"[called tool: {fc.get('name', 'unknown')}({fc.get('args', {})})]"
            )
        fr = part.get("functionResponse") or part.get("function_response")
        if isinstance(fr, dict):
            chunks.append(
                f"[tool {fr.get('name', 'unknown')} returned: {fr.get('response', {})}]"
            )
    return "\n".join(c for c in chunks if c)


def google_contents_to_prompt(
    contents: list[dict[str, Any]] | dict[str, Any] | None,
    system_instruction: Any = None,
) -> PromptBundle:
    """Flatten Google-native ``contents`` (+ optional ``systemInstruction``) into
    the single Gemini prompt string, images split out (SRS 2.4)."""
    images: list[ImageRef] = []
    sections: list[str] = []

    sys_text = ""
    if isinstance(system_instruction, dict):
        sys_text = _google_parts_text(system_instruction.get("parts"), images)
    elif isinstance(system_instruction, str):
        sys_text = system_instruction
    if sys_text:
        sections.append(f"System:\n{sys_text}".rstrip())

    if isinstance(contents, dict):
        contents = [contents]
    for item in contents or []:
        if isinstance(item, str):
            sections.append(f"User:\n{item}")
            continue
        if not isinstance(item, dict):
            continue
        role = (item.get("role") or "user").lower()
        label = _GOOGLE_ROLE_LABELS.get(role, role.capitalize())
        text = _google_parts_text(item.get("parts"), images)
        if text:
            sections.append(f"{label}:\n{text}".rstrip())

    prompt = "\n\n".join(sections).strip() or "(empty conversation)"
    return PromptBundle(prompt=prompt, images=images)


def _responses_content_text(content: Any, images: list[ImageRef]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
                continue
            if not isinstance(part, dict):
                chunks.append(str(part))
                continue
            ptype = part.get("type")
            if ptype in ("input_text", "output_text", "text", "summary_text") or "text" in part:
                chunks.append(str(part.get("text", "")))
            elif ptype in ("input_image", "image_url", "image"):
                img = part.get("image_url")
                url = (
                    img.get("url")
                    if isinstance(img, dict)
                    else img or part.get("image_url") or part.get("url")
                )
                if url:
                    images.append(ImageRef(url=str(url), detail=part.get("detail")))
        return "\n".join(c for c in chunks if c)
    return str(content)


def responses_input_to_prompt(
    input_value: Any, instructions: Any = None
) -> PromptBundle:
    """Flatten an OpenAI Responses API ``input`` (+ ``instructions``) into the
    single Gemini prompt (SRS 2.3). ``input`` may be a bare string or a list of
    typed items (``message`` / ``function_call`` / ``function_call_output``)."""
    images: list[ImageRef] = []
    sections: list[str] = []

    if isinstance(instructions, str) and instructions.strip():
        sections.append(f"System:\n{instructions.strip()}")
    elif isinstance(instructions, list):
        t = _responses_content_text(instructions, images)
        if t:
            sections.append(f"System:\n{t}")

    if isinstance(input_value, str):
        sections.append(f"User:\n{input_value}")
        input_value = []
    if isinstance(input_value, dict):
        input_value = [input_value]

    for item in input_value or []:
        if isinstance(item, str):
            sections.append(f"User:\n{item}")
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")
        if itype in ("function_call",):
            name = item.get("name", "unknown")
            args = item.get("arguments", "")
            sections.append(f"Assistant:\n[called tool: {name}({args})]")
        elif itype in ("function_call_output", "tool_result"):
            out = item.get("output", "")
            cid = item.get("call_id") or item.get("tool_call_id") or "tool"
            sections.append(f"Tool result:\n({cid}) {out}")
        else:  # "message" or shorthand {role, content}
            role = (item.get("role") or "user").lower()
            label = _ROLE_LABELS.get(role, role.capitalize())
            text = _responses_content_text(item.get("content"), images)
            if text:
                sections.append(f"{label}:\n{text}".rstrip())

    prompt = "\n\n".join(sections).strip() or "(empty conversation)"
    return PromptBundle(prompt=prompt, images=images)


def messages_to_prompt(messages: list[dict[str, Any]]) -> PromptBundle:
    images: list[ImageRef] = []
    sections: list[str] = []
    unsupported = False

    for msg in messages:
        role = (msg.get("role") or "user").lower()
        label = _ROLE_LABELS.get(role, role.capitalize())
        text = _text_from_content(msg.get("content"), images)

        # Assistant tool calls / tool responses: keep a legible textual trace so
        # multi-turn context survives even before native tool-calling (Phase 5).
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            rendered = []
            for tc in tool_calls:
                fn = (tc or {}).get("function", {})
                rendered.append(
                    f"{fn.get('name', 'unknown')}({fn.get('arguments', '')})"
                )
            call_text = "[called tools: " + "; ".join(rendered) + "]"
            text = f"{text}\n{call_text}".strip() if text else call_text
        if role in ("tool", "function"):
            name = msg.get("name") or (msg.get("tool_call_id") or "tool")
            text = f"({name}) {text}".strip()

        if not text and not tool_calls:
            continue
        sections.append(f"{label}:\n{text}".rstrip())

    prompt = "\n\n".join(sections).strip()
    if not prompt:
        prompt = "(empty conversation)"
    return PromptBundle(prompt=prompt, images=images, had_unsupported_parts=unsupported)
