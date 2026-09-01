"""Shared generation plumbing used by every wire format (SRS 2.3, 2.4).

Resolves the model, runs the prompt through the shared Gemini client (streaming or
not), and always reports which model actually served the request from the
validated resolution -- never from the model's own text (SRS 2.6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from gemini_webapi.exceptions import GeminiError

from .errors import UpstreamError, classify_upstream
from .gemini_service import GeminiService
from .media import OutputImage, PreparedInputImages, encode_output_images
from .model_selection import ModelNotAvailable, ResolvedModel, available_model_names, resolve
from .translation import PromptBundle

logger = logging.getLogger("gemini_proxy.generation")


@dataclass
class GenerationResult:
    text: str
    resolved: ResolvedModel
    chat_metadata: list[str] = field(default_factory=list)
    images: list[OutputImage] = field(default_factory=list)
    input_image_errors: list[str] = field(default_factory=list)


def _prepared_input(service: GeminiService, bundle: PromptBundle) -> PreparedInputImages:
    cfg = service._config  # noqa: SLF001
    return PreparedInputImages(
        bundle.images,
        fetch_timeout=float(getattr(cfg, "image_fetch_timeout", 20.0)),
        max_bytes=int(getattr(cfg, "max_image_bytes", 20 * 1024 * 1024)),
    )


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
) -> GenerationResult:
    client, resolved = await _resolve(service, requested_model)
    async with _prepared_input(service, bundle) as prepared:
        try:
            output = await client.generate_content(
                bundle.prompt,
                files=prepared.paths or None,
                model=resolved.model,
                temporary=temporary,
                extended_thinking=resolved.extended_thinking,
            )
        except GeminiError as exc:
            raise classify_upstream(exc) from exc
    return GenerationResult(
        text=output.text or "",
        resolved=resolved,
        chat_metadata=list(output.metadata or []),
        images=await encode_output_images(list(getattr(output, "images", []) or [])),
        input_image_errors=prepared.errors,
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
    raw_images: list = []
    async with _prepared_input(service, bundle) as prepared:
        try:
            stream = client.generate_content_stream(
                bundle.prompt,
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
    yield "", final
