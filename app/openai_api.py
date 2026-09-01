"""OpenAI-compatible Chat Completions endpoints (SRS 2.3).

``GET /v1/models`` and ``POST /v1/chat/completions`` (non-streaming + SSE), with
message-to-prompt translation, live model resolution, served-model metadata,
multimodal input/output and prompt-engineered tool calling.

The Responses API (``POST /v1/responses``) is a separate surface in
``app/responses_api.py``.
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
    ToolContext,
    run_generation,
    served_model_metadata,
    stream_generation,
)
from .errors import UpstreamError
from .gemini_service import GeminiService
from .model_selection import ModelNotAvailable
from .sessions_api import resolve_session
from .tools import choice_from_openai, tools_from_openai
from .translation import messages_to_prompt

logger = logging.getLogger("gemini_proxy.openai")

router = APIRouter(tags=["openai"])

META_KEY = "x_gemini_proxy"


def _service(request: Request) -> GeminiService:
    return request.app.state.gemini


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _images_payload(result: GenerationResult) -> list[dict[str, Any]]:
    """Generated/referenced images, base64-encoded directly in the response
    (SRS 2.5) -- OpenAI has no native field, so this is a namespaced extension."""
    return [
        {
            "type": "image",
            "mime_type": img.mime_type,
            "data": img.data,
            "source_url": img.source_url,
        }
        for img in result.images
    ]


def _model_not_available(exc: ModelNotAvailable) -> JSONResponse:
    code = "model_unavailable" if exc.reason == "guest_tier" else "model_not_found"
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "param": "model",
                "code": code,
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


def _tool_calls_payload(result: GenerationResult) -> list[dict[str, Any]]:
    return [
        {
            "id": tc.call_id,
            "type": "function",
            "function": {"name": tc.name, "arguments": tc.arguments_json},
        }
        for tc in result.tool_calls
    ]


def _build_completion(
    service: GeminiService, model_field: str, result: GenerationResult
) -> dict[str, Any]:
    prompt_meta = served_model_metadata(service, result)
    completion_tokens = _rough_tokens(result.text)
    images = _images_payload(result)
    tool_calls = _tool_calls_payload(result)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": (result.text or None) if tool_calls else result.text,
    }
    if images:
        message["images"] = images
    if tool_calls:
        message["tool_calls"] = tool_calls
    payload: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.resolved.served_name,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
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
    if images:
        payload["images"] = images
    return payload


async def _chat_stream(
    service: GeminiService,
    requested_model: str,
    bundle,
    temporary: bool,
    tools: ToolContext | None = None,
    chat: Any = None,
    session: Any = None,
    warm_mgr: Any = None,
):
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    served_name = requested_model
    final_result: GenerationResult | None = None
    suppress_deltas = bool(tools and tools.active)

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
            service, requested_model, bundle, temporary=temporary, tools=tools,
            surface="chat.completions", chat=chat,
        ):
            final_result = running
            served_name = running.resolved.served_name
            if delta_text and not suppress_deltas:
                yield frame({"content": delta_text}, None)
        if session is not None and warm_mgr is not None:
            warm_mgr.touch(session)
    except ModelNotAvailable as exc:
        logger.warning("stream rejected: %s", exc)
        _code = "model_unavailable" if exc.reason == "guest_tier" else "model_not_found"
        yield f"data: {json.dumps({'error': {'message': str(exc), 'code': _code, 'available_models': exc.available}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    except UpstreamError as exc:
        logger.warning("stream upstream error [%s]: %s", exc.code, exc.message)
        yield f"data: {json.dumps({'error': {'message': exc.message, 'type': 'upstream_error', 'code': exc.code}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("streaming generation failed unexpectedly")
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'internal_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    extra = None
    finish = "stop"
    if final_result is not None:
        extra = {META_KEY: served_model_metadata(service, final_result)}
        images = _images_payload(final_result)
        if images:
            extra["images"] = images
        tool_calls = _tool_calls_payload(final_result)
        if tool_calls:
            finish = "tool_calls"
            if suppress_deltas and final_result.text:
                yield frame({"content": final_result.text}, None)
            yield frame({"tool_calls": tool_calls}, None)
    yield frame({}, finish, extra)
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
    session = resolve_session(request, body.get("session_id"))
    requested_model = session.model_name if session else (body.get("model") or cfg.default_model)
    temporary = bool(body.get("temporary_chat", cfg.temporary_chat_default))
    stream = bool(body.get("stream", False))

    bundle = messages_to_prompt(messages, for_session=session is not None)
    if bundle.images:
        logger.info("attaching %d input image(s)", len(bundle.images))

    tools = ToolContext(
        specs=tools_from_openai(body.get("tools")),
        choice=choice_from_openai(body.get("tool_choice")),
    )
    if tools.specs:
        logger.info("prompt-injecting %d tool(s), choice=%s", len(tools.specs), tools.choice.mode)

    chat = session.chat if session else None
    if stream:
        return StreamingResponse(
            _chat_stream(service, requested_model, bundle, temporary, tools, chat, session,
                         request.app.state.warm_sessions),
            media_type="text/event-stream",
        )

    try:
        result = await run_generation(
            service, requested_model, bundle, temporary=temporary, tools=tools,
            surface="chat.completions", chat=chat,
        )
        if session:
            request.app.state.warm_sessions.touch(session)
    except ModelNotAvailable as exc:
        logger.warning("request rejected: %s", exc)
        return _model_not_available(exc)
    except UpstreamError as exc:
        logger.warning("upstream error [%s]: %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.message, "type": "upstream_error", "code": exc.code}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("generation failed unexpectedly")
        raise HTTPException(status_code=502, detail=f"Unexpected error: {exc}")

    return _build_completion(service, requested_model, result)
