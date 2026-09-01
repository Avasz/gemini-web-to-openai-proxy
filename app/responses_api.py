"""OpenAI **Responses API** compatibility (SRS 2.3 / build phase 6).

A distinct surface from Chat Completions, not an alias:
  * request uses a flat ``input`` (string or typed-item array) + ``instructions``
  * response is a ``response`` object with an ``output`` item array
  * streaming uses named SSE events (``response.output_text.delta`` etc.)

Generation plumbing, model resolution, images and tool parsing are shared with
the other surfaces via ``app/generation.py``.
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
from .tools import choice_from_openai, tools_from_openai
from .translation import responses_input_to_prompt

logger = logging.getLogger("gemini_proxy.responses")

router = APIRouter(tags=["openai-responses"])

META_KEY = "x_gemini_proxy"


def _service(request: Request) -> GeminiService:
    return request.app.state.gemini


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _output_items(result: GenerationResult) -> tuple[list[dict[str, Any]], str]:
    """Build the Responses ``output`` array and the aggregated ``output_text``."""
    items: list[dict[str, Any]] = []
    text = result.text or ""
    if text or not result.tool_calls:
        items.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": text, "annotations": []}
                ],
            }
        )
    for tc in result.tool_calls:
        items.append(
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": tc.call_id,
                "name": tc.name,
                "arguments": tc.arguments_json,
                "status": "completed",
            }
        )
    return items, text


def _response_object(
    service: GeminiService,
    result: GenerationResult,
    *,
    request_body: dict[str, Any],
    status: str,
    output: list[dict[str, Any]],
    output_text: str,
) -> dict[str, Any]:
    out_tokens = _rough_tokens(output_text)
    obj: dict[str, Any] = {
        "id": request_body.get("_resp_id") or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": result.resolved.served_name,
        "output": output,
        "output_text": output_text,
        "usage": {
            "input_tokens": 0,
            "output_tokens": out_tokens,
            "total_tokens": out_tokens,
        },
        "parallel_tool_calls": bool(request_body.get("parallel_tool_calls", True)),
        "tool_choice": request_body.get("tool_choice", "auto"),
        "tools": request_body.get("tools", []) or [],
        "instructions": request_body.get("instructions"),
        "metadata": request_body.get("metadata") or {},
        META_KEY: served_model_metadata(service, result),
    }
    return obj


def _parse_request(body: dict[str, Any], cfg) -> tuple[str, bool, bool, ToolContext, Any]:
    requested_model = body.get("model") or cfg.default_model
    stream = bool(body.get("stream", False))
    temporary = bool(body.get("temporary_chat", cfg.temporary_chat_default))
    tools = ToolContext(
        specs=tools_from_openai(body.get("tools")),
        choice=choice_from_openai(body.get("tool_choice")),
    )
    bundle = responses_input_to_prompt(body.get("input"), body.get("instructions"))
    return requested_model, stream, temporary, tools, bundle


@router.post("/v1/responses")
async def create_response(
    request: Request, _: None = Depends(require_api_key)
) -> Any:
    service = _service(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    if body.get("input") is None and not body.get("instructions"):
        raise HTTPException(status_code=400, detail="'input' is required.")

    cfg = request.app.state.config
    requested_model, stream, temporary, tools, bundle = _parse_request(body, cfg)
    if bundle.images:
        logger.info("attaching %d input image(s)", len(bundle.images))
    if tools.specs:
        logger.info("prompt-injecting %d tool(s)", len(tools.specs))

    if stream:
        return StreamingResponse(
            _response_stream(service, requested_model, bundle, temporary, tools, body),
            media_type="text/event-stream",
        )

    try:
        result = await run_generation(
            service, requested_model, bundle, temporary=temporary, tools=tools,
            surface="responses"
        )
    except ModelNotAvailable as exc:
        logger.warning("responses request rejected: %s", exc)
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
    except UpstreamError as exc:
        logger.warning("responses upstream error [%s]: %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.message, "type": "upstream_error", "code": exc.code}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("responses generation failed unexpectedly")
        raise HTTPException(status_code=502, detail=f"Unexpected error: {exc}")

    output, output_text = _output_items(result)
    return _response_object(
        service, result, request_body=body, status="completed",
        output=output, output_text=output_text,
    )


async def _response_stream(
    service: GeminiService,
    requested_model: str,
    bundle,
    temporary: bool,
    tools: ToolContext,
    body: dict[str, Any],
):
    resp_id = f"resp_{uuid.uuid4().hex}"
    body = {**body, "_resp_id": resp_id}
    seq = 0
    suppress = bool(tools and tools.active)

    def event(name: str, payload: dict[str, Any]) -> str:
        nonlocal seq
        payload = {"type": name, "sequence_number": seq, **payload}
        seq += 1
        return f"event: {name}\ndata: {json.dumps(payload)}\n\n"

    # skeleton response object for the created / in_progress events
    def skeleton(status: str, output: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": requested_model,
            "output": output,
            "output_text": "",
        }

    yield event("response.created", {"response": skeleton("in_progress", [])})
    yield event("response.in_progress", {"response": skeleton("in_progress", [])})

    msg_id = f"msg_{uuid.uuid4().hex}"
    text_started = False
    acc = ""
    final: GenerationResult | None = None

    try:
        async for delta_text, running in stream_generation(
            service, requested_model, bundle, temporary=temporary, tools=tools,
            surface="responses"
        ):
            final = running
            if not delta_text or suppress:
                continue
            if not text_started:
                text_started = True
                yield event(
                    "response.output_item.added",
                    {
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "id": msg_id,
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                )
                yield event(
                    "response.content_part.added",
                    {
                        "item_id": msg_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            acc += delta_text
            yield event(
                "response.output_text.delta",
                {
                    "item_id": msg_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": delta_text,
                },
            )
    except ModelNotAvailable as exc:
        yield event("response.failed", {"response": {
            **skeleton("failed", []),
            "error": {"code": "model_not_found", "message": str(exc),
                      "available_models": exc.available},
        }})
        return
    except UpstreamError as exc:
        yield event("response.failed", {"response": {
            **skeleton("failed", []),
            "error": {"code": exc.code, "message": exc.message},
        }})
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("responses streaming failed unexpectedly")
        yield event("response.failed", {"response": {
            **skeleton("failed", []),
            "error": {"code": "internal_error", "message": str(exc)},
        }})
        return

    if final is None:
        final = GenerationResult(text="", resolved=(await _cheap_resolved(service, requested_model)))

    # close out the streamed text item (or emit it whole if suppressed)
    full_text = final.text or ""
    if suppress and full_text:
        # tools active: we withheld deltas; emit the residual prose now
        yield event("response.output_item.added", {
            "output_index": 0,
            "item": {"type": "message", "id": msg_id, "status": "in_progress",
                     "role": "assistant", "content": []},
        })
        yield event("response.content_part.added", {
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })
        yield event("response.output_text.delta", {
            "item_id": msg_id, "output_index": 0, "content_index": 0, "delta": full_text,
        })
        text_started = True

    if text_started:
        yield event("response.output_text.done", {
            "item_id": msg_id, "output_index": 0, "content_index": 0, "text": full_text or acc,
        })
        yield event("response.content_part.done", {
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": full_text or acc, "annotations": []},
        })
        yield event("response.output_item.done", {
            "output_index": 0,
            "item": {
                "type": "message", "id": msg_id, "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text or acc, "annotations": []}],
            },
        })

    idx = 1 if text_started else 0
    for tc in final.tool_calls:
        fc_id = f"fc_{uuid.uuid4().hex}"
        yield event("response.output_item.added", {
            "output_index": idx,
            "item": {"type": "function_call", "id": fc_id, "call_id": tc.call_id,
                     "name": tc.name, "arguments": "", "status": "in_progress"},
        })
        yield event("response.function_call_arguments.delta", {
            "item_id": fc_id, "output_index": idx, "delta": tc.arguments_json,
        })
        yield event("response.function_call_arguments.done", {
            "item_id": fc_id, "output_index": idx, "arguments": tc.arguments_json,
        })
        yield event("response.output_item.done", {
            "output_index": idx,
            "item": {"type": "function_call", "id": fc_id, "call_id": tc.call_id,
                     "name": tc.name, "arguments": tc.arguments_json, "status": "completed"},
        })
        idx += 1

    output, output_text = _output_items(final)
    completed = _response_object(
        service, final, request_body=body, status="completed",
        output=output, output_text=output_text,
    )
    yield event("response.completed", {"response": completed})


async def _cheap_resolved(service: GeminiService, requested_model: str):
    from .model_selection import resolve

    client = await service.get_client()
    return resolve(client, requested_model)
