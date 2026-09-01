"""Shared generation plumbing used by every wire format (SRS 2.3, 2.4).

Resolves the model, runs the prompt through the shared Gemini client (streaming or
not), and always reports which model actually served the request from the
validated resolution -- never from the model's own text (SRS 2.6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .gemini_service import GeminiService
from .model_selection import ResolvedModel, resolve
from .translation import PromptBundle

logger = logging.getLogger("gemini_proxy.generation")


@dataclass
class GenerationResult:
    text: str
    resolved: ResolvedModel
    chat_metadata: list[str] = field(default_factory=list)
    images: list[Any] = field(default_factory=list)


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
    return _served_model_meta(service, result.resolved, result.chat_metadata)


async def _resolve(service: GeminiService, requested: str) -> tuple[Any, ResolvedModel]:
    client = await service.get_client()
    return client, resolve(client, requested)


async def run_generation(
    service: GeminiService,
    requested_model: str,
    bundle: PromptBundle,
    *,
    temporary: bool,
) -> GenerationResult:
    client, resolved = await _resolve(service, requested_model)
    output = await client.generate_content(
        bundle.prompt,
        model=resolved.model,
        temporary=temporary,
        extended_thinking=resolved.extended_thinking,
    )
    return GenerationResult(
        text=output.text or "",
        resolved=resolved,
        chat_metadata=list(output.metadata or []),
        images=list(getattr(output, "images", []) or []),
    )


async def stream_generation(
    service: GeminiService,
    requested_model: str,
    bundle: PromptBundle,
    *,
    temporary: bool,
) -> AsyncIterator[tuple[str, GenerationResult]]:
    """Yield ``(delta_text, running_result)`` tuples. The final tuple carries the
    complete text and metadata."""
    client, resolved = await _resolve(service, requested_model)
    last_full = ""
    final: GenerationResult | None = None
    async for output in client.generate_content_stream(
        bundle.prompt,
        model=resolved.model,
        temporary=temporary,
        extended_thinking=resolved.extended_thinking,
    ):
        full = output.text or ""
        delta = output.text_delta or ""
        if not delta and len(full) > len(last_full):
            delta = full[len(last_full) :]
        last_full = full if len(full) >= len(last_full) else last_full
        final = GenerationResult(
            text=last_full,
            resolved=resolved,
            chat_metadata=list(output.metadata or []),
            images=list(getattr(output, "images", []) or []),
        )
        if delta:
            yield delta, final
    if final is None:
        final = GenerationResult(text="", resolved=resolved)
    yield "", final
