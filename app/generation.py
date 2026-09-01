"""Shared generation plumbing used by every wire format (SRS 2.3, 2.4).

Resolves the model, runs the prompt through the shared Gemini client (streaming or
not), and always reports which model actually served the request from the
validated resolution -- never from the model's own text (SRS 2.6).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from gemini_webapi.exceptions import GeminiError

from .activity_log import RequestRecord
from .errors import UpstreamError, classify_upstream
from .gemini_service import GeminiService
from .media import OutputImage, PreparedInputImages, encode_output_images
from .model_selection import ModelNotAvailable, ResolvedModel, available_model_names, resolve
from .tools import ParsedToolCall, ToolChoice, ToolSpec, build_tool_instructions, parse_tool_calls
from .translation import PromptBundle

logger = logging.getLogger("gemini_proxy.generation")


@dataclass
class ToolContext:
    specs: list[ToolSpec] = field(default_factory=list)
    choice: ToolChoice = field(default_factory=ToolChoice)

    @property
    def active(self) -> bool:
        return bool(self.specs) and self.choice.mode != "none"


@dataclass
class GenerationResult:
    text: str
    resolved: ResolvedModel
    chat_metadata: list[str] = field(default_factory=list)
    images: list[OutputImage] = field(default_factory=list)
    input_image_errors: list[str] = field(default_factory=list)
    tool_calls: list[ParsedToolCall] = field(default_factory=list)


def _record(
    service: GeminiService,
    *,
    surface: str,
    requested: str,
    started: float,
    prompt_chars: int,
    streamed: bool,
    result: "GenerationResult | None",
    error_code: str | None,
) -> None:
    activity = getattr(service, "activity", None)
    if activity is None:
        return
    activity.record(
        RequestRecord(
            ts=time.time(),
            surface=surface,
            model_requested=requested,
            model_served=result.resolved.served_name if result else None,
            ok=error_code is None,
            error_code=error_code,
            latency_ms=round((time.time() - started) * 1000, 1),
            prompt_chars=prompt_chars,
            reply_chars=len(result.text) if result else 0,
            streamed=streamed,
        )
    )


def _prepared_input(service: GeminiService, bundle: PromptBundle) -> PreparedInputImages:
    cfg = service._config  # noqa: SLF001
    return PreparedInputImages(
        bundle.images,
        fetch_timeout=float(getattr(cfg, "image_fetch_timeout", 20.0)),
        max_bytes=int(getattr(cfg, "max_image_bytes", 20 * 1024 * 1024)),
    )


def _prompt_with_tools(bundle: PromptBundle, tools: ToolContext | None) -> str:
    if not tools or not tools.active:
        return bundle.prompt
    block = build_tool_instructions(tools.specs, tools.choice)
    if not block:
        return bundle.prompt
    # Put the tool contract AFTER the conversation -- Gemini weights the tail of
    # the prompt heavily, and a leading instruction block gets ignored once the
    # user's actual question follows it.
    return f"{bundle.prompt}\n\n---\n\n{block}"


def _finalise_tools(result: GenerationResult, tools: ToolContext | None) -> GenerationResult:
    if tools and tools.active:
        calls, visible = parse_tool_calls(result.text)
        result.tool_calls = calls
        if calls:
            result.text = visible
    return result


def _served_model_meta(
    service: GeminiService, resolved: ResolvedModel, chat_metadata: list[str]
) -> dict[str, Any]:
    client = service._client  # noqa: SLF001
    meta: dict[str, Any] = {
        "requested_model": resolved.requested,
        "served_model": resolved.served_name,
        "model_id": resolved.model.model_id,
        "extended_thinking": resolved.extended_thinking,
        "cookie_mode": service.cookie_mode,
        "chat_metadata": chat_metadata,
    }
    if client is not None:
        try:
            if client.usage_info:
                meta["usage_info"] = client.usage_info
            elif client.quotas:
                meta["quotas"] = client.quotas
        except Exception:  # noqa: BLE001
            pass
    return meta


def served_model_metadata(
    service: GeminiService, result: GenerationResult
) -> dict[str, Any]:
    meta = _served_model_meta(service, result.resolved, result.chat_metadata)
    if result.input_image_errors:
        meta["input_image_errors"] = result.input_image_errors
    if result.images:
        meta["output_image_count"] = len(result.images)
    if result.tool_calls:
        meta["tool_call_count"] = len(result.tool_calls)
    return meta


async def _resolve(service: GeminiService, requested: str) -> tuple[Any, ResolvedModel]:
    client = await service.get_client()
    resolved = resolve(client, requested)
    # Fail before the network call when the account plainly can't use this model
    # (e.g. a non-default model on a guest session). Same signal the library would
    # raise GeminiError on, surfaced as a clean 4xx instead of a traceback.
    if not resolved.model.is_available:
        usable = [
            m.model_name for m in (client.list_models() or []) if m.is_available
        ]
        raise ModelNotAvailable(
            requested, usable or available_model_names(client), reason="guest_tier"
        )
    return client, resolved


async def run_generation(
    service: GeminiService,
    requested_model: str,
    bundle: PromptBundle,
    *,
    temporary: bool,
    tools: ToolContext | None = None,
    surface: str = "",
) -> GenerationResult:
    started = time.time()
    prompt_chars = len(bundle.prompt)
    try:
        client, resolved = await _resolve(service, requested_model)
        prompt = _prompt_with_tools(bundle, tools)
        async with _prepared_input(service, bundle) as prepared:
            try:
                output = await client.generate_content(
                    prompt,
                    files=prepared.paths or None,
                    model=resolved.model,
                    temporary=temporary,
                    extended_thinking=resolved.extended_thinking,
                )
            except GeminiError as exc:
                raise classify_upstream(exc) from exc
        result = GenerationResult(
            text=output.text or "",
            resolved=resolved,
            chat_metadata=list(output.metadata or []),
            images=await encode_output_images(list(getattr(output, "images", []) or [])),
            input_image_errors=prepared.errors,
        )
        _finalise_tools(result, tools)
    except (ModelNotAvailable, UpstreamError) as exc:
        code = getattr(exc, "code", None) or "model_not_available"
        _record(service, surface=surface, requested=requested_model, started=started,
                prompt_chars=prompt_chars, streamed=False, result=None, error_code=code)
        raise
    except Exception:
        _record(service, surface=surface, requested=requested_model, started=started,
                prompt_chars=prompt_chars, streamed=False, result=None, error_code="internal")
        raise
    _record(service, surface=surface, requested=requested_model, started=started,
            prompt_chars=prompt_chars, streamed=False, result=result, error_code=None)
    return result


async def stream_generation(
    service: GeminiService,
    requested_model: str,
    bundle: PromptBundle,
    *,
    temporary: bool,
    tools: ToolContext | None = None,
    surface: str = "",
) -> AsyncIterator[tuple[str, GenerationResult]]:
    """Yield ``(delta_text, running_result)`` tuples. The final tuple carries the
    complete text, tool calls and metadata.

    When tools are active the caller should ignore the intermediate deltas (a
    tool-call block spans several of them) and act on the final tuple only.
    """
    started = time.time()
    prompt_chars = len(bundle.prompt)
    last_full = ""
    final: GenerationResult | None = None
    raw_images: list = []
    try:
        client, resolved = await _resolve(service, requested_model)
        prompt = _prompt_with_tools(bundle, tools)
        async with _prepared_input(service, bundle) as prepared:
            try:
                stream = client.generate_content_stream(
                    prompt,
                    files=prepared.paths or None,
                    model=resolved.model,
                    temporary=temporary,
                    extended_thinking=resolved.extended_thinking,
                )
                async for output in stream:
                    full = output.text or ""
                    delta = output.text_delta or ""
                    if not delta and len(full) > len(last_full):
                        delta = full[len(last_full) :]
                    if len(full) >= len(last_full):
                        last_full = full
                    raw_images = list(getattr(output, "images", []) or []) or raw_images
                    final = GenerationResult(
                        text=last_full,
                        resolved=resolved,
                        chat_metadata=list(output.metadata or []),
                        input_image_errors=prepared.errors,
                    )
                    if delta:
                        yield delta, final
            except GeminiError as exc:
                raise classify_upstream(exc) from exc
        if final is None:
            final = GenerationResult(text="", resolved=resolved,
                                     input_image_errors=prepared.errors)
        final.images = await encode_output_images(raw_images)
        _finalise_tools(final, tools)
    except (ModelNotAvailable, UpstreamError) as exc:
        _record(service, surface=surface, requested=requested_model, started=started,
                prompt_chars=prompt_chars, streamed=True, result=None,
                error_code=getattr(exc, "code", None) or "model_not_available")
        raise
    except Exception:
        _record(service, surface=surface, requested=requested_model, started=started,
                prompt_chars=prompt_chars, streamed=True, result=None, error_code="internal")
        raise
    _record(service, surface=surface, requested=requested_model, started=started,
            prompt_chars=prompt_chars, streamed=True, result=final, error_code=None)
    yield "", final
