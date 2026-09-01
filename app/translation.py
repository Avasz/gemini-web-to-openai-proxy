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
