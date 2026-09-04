"""Google-native (Generative Language API) compatible endpoints (SRS 2.4).

  GET  /v1beta/models
  GET  /v1beta/models/{model}
  POST /v1beta/models/{model}:generateContent
  POST /v1beta/models/{model}:streamGenerateContent   (?alt=sse for SSE framing)

Shares model resolution and the generation plumbing with the OpenAI path
(``app/generation.py``); only the request/response shaping differs. Tools and
image *output* land in Phases 5 and 4.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import require_api_key
from .errors import UpstreamError
from .generation import (
    GenerationResult,
    ToolContext,
    run_generation,
    served_model_metadata,
    stream_generation,
)
from .gemini_service import GeminiService
from .model_selection import ModelNotAvailable
from .request_opts import resolve_temporary
from .sessions_api import resolve_session
from .tools import choice_from_google, tools_from_google
from .translation import google_contents_to_prompt

logger = logging.getLogger("gemini_proxy.google")

router = APIRouter(tags=["google-native"])

META_KEY = "x_gemini_proxy"


def _service(request: Request) -> GeminiService:
    return request.app.state.gemini


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _strip_models_prefix(name: str) -> str:
    return name[len("models/"):] if name.startswith("models/") else name


def _model_error_response(exc: ModelNotAvailable) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": 400,
                "message": str(exc),
                "status": "INVALID_ARGUMENT",
                "availableModels": [f"models/{m}" for m in exc.available],
            }
        },
    )


def _model_payload(m: Any) -> dict[str, Any]:
    return {
        "name": f"models/{m.model_name}",
        "baseModelId": m.model_name,
        "displayName": m.display_name,
        "description": m.description,
        "supportedGenerationMethods": [
            "generateContent",
            "streamGenerateContent",
        ],
        "gemini": {"modelId": m.model_id, "aliases": list(m.aliases),
                   "isAvailable": m.is_available},
    }


@router.get("/v1beta/models")
async def list_models(request: Request, _: None = Depends(require_api_key)) -> dict:
    service = _service(request)
    try:
        client = await service.get_client()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Gemini client unavailable: {exc}")
    return {"models": [_model_payload(m) for m in (client.list_models() or [])]}


@router.get("/v1beta/models/{model:path}")
async def get_model(
    model: str, request: Request, _: None = Depends(require_api_key)
) -> dict:
    if ":" in model:  # a mis-routed action verb
        raise HTTPException(status_code=404, detail="Not found.")
    service = _service(request)
    try:
        client = await service.get_client()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Gemini client unavailable: {exc}")
    name = _strip_models_prefix(model)
    for m in client.list_models() or []:
        if m.model_name == name or name in m.aliases:
            return _model_payload(m)
    raise HTTPException(status_code=404, detail=f"Model 'models/{name}' not found.")


def _image_parts(result: GenerationResult) -> list[dict[str, Any]]:
    """Generated images as native Google ``inlineData`` parts (SRS 2.5)."""
    return [
        {"inlineData": {"mimeType": img.mime_type, "data": img.data}}
        for img in result.images
    ]


def _images_payload(result: GenerationResult) -> list[dict[str, Any]]:
    return [
        {"mimeType": img.mime_type, "data": img.data, "sourceUrl": img.source_url}
        for img in result.images
    ]


def _function_call_parts(result: GenerationResult) -> list[dict[str, Any]]:
    return [
        {"functionCall": {"name": tc.name, "args": tc.arguments}}
        for tc in result.tool_calls
    ]


def _generate_content_response(
    service: GeminiService, result: GenerationResult
) -> dict[str, Any]:
    completion_tokens = _rough_tokens(result.text)
    parts: list[dict[str, Any]] = []
    if result.text:
        parts.append({"text": result.text})
    parts.extend(_image_parts(result))
    parts.extend(_function_call_parts(result))
    if not parts:
        parts.append({"text": ""})
    payload: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 0,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": completion_tokens,
        },
        "modelVersion": result.resolved.served_name,
        META_KEY: served_model_metadata(service, result),
    }
    if result.images:
        payload["images"] = _images_payload(result)
    return payload


def _chunk_response(text: str, served_name: str) -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}
        ],
        "modelVersion": served_name,
    }


async def _stream_google(
    service: GeminiService,
    requested_model: str,
    bundle,
    temporary: bool,
    sse: bool,
    tools: ToolContext | None = None,
    chat: Any = None,
):
    served_name = requested_model
    final: GenerationResult | None = None
    suppress_deltas = bool(tools and tools.active)

    def emit(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n" if sse else json.dumps(obj)

    if not sse:
        yield "["
    first = True
    try:
        async for delta_text, running in stream_generation(
            service, requested_model, bundle, temporary=temporary, tools=tools,
            surface="streamGenerateContent", chat=chat,
        ):
            final = running
            served_name = running.resolved.served_name
            if not delta_text or suppress_deltas:
                continue
            if not sse and not first:
                yield ","
            first = False
            yield emit(_chunk_response(delta_text, served_name))
    except (ModelNotAvailable, UpstreamError) as exc:
        msg = str(exc) if isinstance(exc, ModelNotAvailable) else exc.message
        status = "INVALID_ARGUMENT" if isinstance(exc, ModelNotAvailable) else "UNAVAILABLE"
        logger.warning("google stream rejected: %s", msg)
        err = {"error": {"code": 400, "message": msg, "status": status}}
        if not sse and not first:
            yield ","
        yield emit(err)
        if not sse:
            yield "]"
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("google streaming failed unexpectedly")
        err = {"error": {"code": 500, "message": str(exc), "status": "INTERNAL"}}
        if not sse and not first:
            yield ","
        yield emit(err)
        if not sse:
            yield "]"
        return

    # Final chunk: finishReason + usage + served-model metadata (+ any images/calls)
    tail_parts: list[dict[str, Any]] = []
    if final is not None:
        if suppress_deltas and final.text:
            tail_parts.append({"text": final.text})
        tail_parts.extend(_image_parts(final))
        tail_parts.extend(_function_call_parts(final))
    tail: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": tail_parts},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "modelVersion": served_name,
    }
    if final is not None:
        ct = _rough_tokens(final.text)
        tail["usageMetadata"] = {
            "promptTokenCount": 0,
            "candidatesTokenCount": ct,
            "totalTokenCount": ct,
        }
        tail[META_KEY] = served_model_metadata(service, final)
        if final.images:
            tail["images"] = _images_payload(final)
    if not sse and not first:
        yield ","
    yield emit(tail)
    if not sse:
        yield "]"


@router.post("/v1beta/models/{spec:path}")
async def generate(
    spec: str, request: Request, _: None = Depends(require_api_key)
) -> Any:
    if ":" not in spec:
        raise HTTPException(
            status_code=400,
            detail="Expected /v1beta/models/<model>:generateContent or :streamGenerateContent",
        )
    model_part, _, action = spec.rpartition(":")
    requested_model = _strip_models_prefix(model_part)
    if action not in ("generateContent", "streamGenerateContent"):
        raise HTTPException(status_code=404, detail=f"Unknown method ':{action}'.")

    service = _service(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    cfg = request.app.state.config
    if not requested_model:
        requested_model = cfg.default_model
    temporary = resolve_temporary(request, body.get("temporaryChat"), cfg)
    session = resolve_session(request, body.get("sessionId") or body.get("session_id"))
    if session:
        requested_model = session.model_name
    chat = session.chat if session else None

    bundle = google_contents_to_prompt(
        body.get("contents"),
        body.get("systemInstruction") or body.get("system_instruction"),
        for_session=session is not None,
    )
    if bundle.images:
        logger.info("attaching %d input image(s)", len(bundle.images))

    tools = ToolContext(
        specs=tools_from_google(body.get("tools")),
        choice=choice_from_google(body.get("toolConfig") or body.get("tool_config")),
    )
    if tools.specs:
        logger.info("prompt-injecting %d tool(s), mode=%s", len(tools.specs), tools.choice.mode)

    if action == "streamGenerateContent":
        sse = request.query_params.get("alt", "").lower() == "sse"
        return StreamingResponse(
            _stream_google(service, requested_model, bundle, temporary, sse, tools, chat),
            media_type="text/event-stream" if sse else "application/json",
        )

    try:
        result = await run_generation(
            service, requested_model, bundle, temporary=temporary, tools=tools,
            surface="generateContent", chat=chat,
        )
        if session:
            request.app.state.warm_sessions.touch(session)
    except ModelNotAvailable as exc:
        logger.warning("google request rejected: %s", exc)
        return _model_error_response(exc)
    except UpstreamError as exc:
        logger.warning("google upstream error [%s]: %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.status_code, "message": exc.message,
                               "status": exc.code.upper()}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("google generation failed unexpectedly")
        raise HTTPException(status_code=502, detail=f"Unexpected error: {exc}")

    return _generate_content_response(service, result)
