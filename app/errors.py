"""Map upstream ``gemini_webapi`` failures onto clean client-facing errors.

The library raises ``GeminiError`` for a range of conditions -- an unavailable
model on a guest session, an expired session, a usage cap. Those are expected
operational states, not proxy bugs, so we classify them into a small set with
sensible HTTP status codes and log them at WARNING (no stack trace) rather than
dumping a traceback for every one.
"""

from __future__ import annotations

from gemini_webapi.exceptions import (
    AuthError,
    GeminiError,
    ModelInvalidError,
    TemporarilyBlockedError,
    TimeoutError as GeminiTimeoutError,
    UsageLimitExceededError,
)


class UpstreamError(Exception):
    """A classified upstream failure with a client-facing message + status."""

    def __init__(self, message: str, status_code: int, code: str):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def classify_upstream(exc: Exception) -> UpstreamError:
    text = str(exc)
    lowered = text.lower()

    if isinstance(exc, UsageLimitExceededError) or "usage limit" in lowered:
        return UpstreamError(
            "Gemini account usage limit reached. Try again after the quota window resets.",
            429,
            "usage_limit_exceeded",
        )
    if isinstance(exc, TemporarilyBlockedError) or "429" in lowered or "rate" in lowered:
        return UpstreamError(
            "Gemini temporarily rate-limited this client. Back off and retry later.",
            429,
            "rate_limited",
        )
    if isinstance(exc, (AuthError,)) or "unauthenticated" in lowered or "cookies have expired" in lowered:
        return UpstreamError(
            "Gemini reports the session is not fully authenticated "
            "(account status not AVAILABLE). While in this state: non-default models "
            "and file/image uploads are refused; plain-text requests to the default "
            "model may still work. Causes: expired/wrong-account cookies, an "
            "unprovisioned account, or a temporary auth throttle from repeated "
            "re-initialisation -- if cookies are known good, wait and retry cold "
            "rather than restarting.",
            403,
            "session_unauthenticated",
        )
    if isinstance(exc, ModelInvalidError) or "not available for use" in lowered:
        return UpstreamError(
            text or "Requested model is not available for this account.",
            403,
            "model_unavailable",
        )
    if isinstance(exc, GeminiTimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return UpstreamError(
            "Gemini request timed out. The upstream connection may be stalled; retry.",
            504,
            "upstream_timeout",
        )
    if isinstance(exc, GeminiError):
        return UpstreamError(f"Upstream Gemini error: {text}", 502, "upstream_error")

    return UpstreamError(f"Unexpected upstream error: {text}", 502, "upstream_error")
