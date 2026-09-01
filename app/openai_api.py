"""OpenAI-compatible endpoints (SRS 2.3).

Phase 2 scope: ``GET /v1/models``, ``POST /v1/chat/completions`` (non-streaming
and SSE streaming), message-to-prompt translation, live model resolution, and the
served-model response metadata. Images (Phase 4), tools (Phase 5) and
``/v1/responses`` (Phase 6) come later.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import require_api_key
from .generation import (
    GenerationResult,
    run_generation,
    served_model_metadata,
    stream_generation,
)
from .gemini_service import GeminiService
from .model_selection import ModelNotAvailable
from .translation import messages_to_prompt

logger = logging.getLogger("gemini_proxy.openai")

router = APIRouter(tags=["openai"])

META_KEY = "x_gemini_proxy"


def _service(request: Request) -> GeminiService:
    return request.app.state.gemini


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _model_not_available(exc: ModelNotAvailable) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "param": "model",
                "code": "model_not_found",
                "available_models": exc.available,
            }
        },
    )


@router.get("/v1/models")
async def list_models(request: Request, _: None = Depends(require_api_key)) -> dict:
    service = _service(request)
    try:
        client = await service.get_client()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Gemini client unavailable: {exc}")
    now = int(time.time())
    data = []
    for m in client.list_models() or []:
        data.append(
            {
                "id": m.model_name,
                "object": "model",
                "created": now,
                "owned_by": "google-gemini-web",
                "gemini": {
                    "model_id": m.model_id,
                    "display_name": m.display_name,
                    "description": m.description,
                    "aliases": list(m.aliases),
                    "is_available": m.is_available,
                },
            }
        )
    return {"object": "list", "data": data}


def _build_completion(
    service: GeminiService, model_field: str, result: GenerationResult
) -> dict[str, Any]:
    prompt_meta = served_model_metadata(service, result)
    completion_tokens = _rough_tokens(result.text)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.resolved.served_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": completion_tokens,
            "total_tokens": completion_tokens,
            "note": "token counts are rough estimates; Gemini Web reports none",
        },
        META_KEY: prompt_meta,
    }


async def _chat_stream(
    service: GeminiService, requested_model: str, bundle, temporary: bool
):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    served_name = requested_model
    final_result: GenerationResult | None = None

    def frame(delta: dict[str, Any], finish: str | None, extra: dict | None = None):
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": served_name,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            payload.update(extra)
        return f"data: {json.dumps(payload)}\n\n"

    try:
        yield frame({"role": "assistant", "content": ""}, None)
        async for delta_text, running in stream_generation(
            service, requested_model, bundle, temporary=temporary
        ):
            final_result = running
            served_name = running.resolved.served_name
            if delta_text:
                yield frame({"content": delta_text}, None)
    except ModelNotAvailable as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc), 'code': 'model_not_found', 'available_models': exc.available}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("streaming generation failed")
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    extra = None
    if final_result is not None:
        extra = {META_KEY: served_model_metadata(service, final_result)}
    yield frame({}, "stop", extra)
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request, _: None = Depends(require_api_key)
) -> Any:
    service = _service(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="'messages' must be a non-empty array.")

    cfg = request.app.state.config
    requested_model = body.get("model") or cfg.default_model
    temporary = bool(body.get("temporary_chat", cfg.temporary_chat_default))
    stream = bool(body.get("stream", False))

    bundle = messages_to_prompt(messages)
    if bundle.images:
        logger.warning(
            "Ignoring %d image part(s): multimodal input lands in Phase 4.",
            len(bundle.images),
        )
    if body.get("tools"):
        logger.warning("Ignoring 'tools': native tool-calling lands in Phase 5.")

    if stream:
        return StreamingResponse(
            _chat_stream(service, requested_model, bundle, temporary),
            media_type="text/event-stream",
        )

    try:
        result = await run_generation(
            service, requested_model, bundle, temporary=temporary
        )
    except ModelNotAvailable as exc:
        return _model_not_available(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("generation failed")
        raise HTTPException(status_code=502, detail=f"Upstream Gemini error: {exc}")

    return _build_completion(service, requested_model, result)
