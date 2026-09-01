"""Model-name resolution and the reasoning-effort suffix (SRS 2.6).

Callers name a model with an optional reasoning suffix:

    gemini-flash          -> extended thinking OFF
    gemini-flash-high     -> extended thinking ON
    gemini-pro-low        -> extended thinking OFF
    gemini-pro-medium     -> extended thinking OFF

Only ``-high`` turns extended thinking on. ``gemini_webapi`` exposes a single
on/off ``extended_thinking`` flag, so this stays on/off on purpose -- no faked
graded scale (SRS 2.6 / 5).

The base name (suffix stripped) is handed to ``client.resolve_model()``, which
matches against the models THIS account actually discovered. An unknown name
raises ``ModelNotAvailable`` carrying the real list for a clean 4xx.
"""

from __future__ import annotations

from dataclasses import dataclass

from gemini_webapi import GeminiClient
from gemini_webapi.types import AvailableModel

_REASONING_SUFFIXES = {
    "-high": True,
    "-medium": False,
    "-low": False,
    "-minimal": False,
    "-none": False,
}


class ModelNotAvailable(ValueError):
    def __init__(self, requested: str, available: list[str], *, reason: str = "unknown"):
        self.requested = requested
        self.available = available
        self.reason = reason
        if reason == "guest_tier":
            lead = (
                f"Model '{requested}' requires an authenticated account; the current "
                f"session is anonymous/guest and can only use"
            )
        else:
            lead = f"Unknown model '{requested}'. Models available to this account:"
        super().__init__(
            f"{lead} {', '.join(available) if available else '(none discovered yet)'}."
        )


@dataclass(frozen=True)
class ResolvedModel:
    requested: str
    base_name: str
    served_name: str
    extended_thinking: bool
    model: AvailableModel


def split_reasoning_suffix(name: str) -> tuple[str, bool]:
    lowered = name.strip()
    for suffix, thinking in _REASONING_SUFFIXES.items():
        if lowered.lower().endswith(suffix) and len(lowered) > len(suffix):
            return lowered[: -len(suffix)], thinking
    return lowered, False


def available_model_names(client: GeminiClient) -> list[str]:
    models = client.list_models() or []
    return [m.model_name for m in models]


def resolve(client: GeminiClient, requested: str) -> ResolvedModel:
    base_name, extended_thinking = split_reasoning_suffix(requested)
    try:
        model = client.resolve_model(base_name)
    except ValueError as exc:  # library raises plain ValueError
        raise ModelNotAvailable(requested, available_model_names(client)) from exc
    return ResolvedModel(
        requested=requested,
        base_name=base_name,
        served_name=model.model_name,
        extended_thinking=extended_thinking,
        model=model,
    )
