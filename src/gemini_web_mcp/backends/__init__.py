"""Pluggable Gemini backend implementations."""

from gemini_web_mcp.backends.base import (
    AskRequest,
    AskResponse,
    BackendError,
    GeminiBackend,
    HealthStatus,
)
from gemini_web_mcp.backends.cookie_backend import CookieBackend

__all__ = [
    "AskRequest",
    "AskResponse",
    "BackendError",
    "CookieBackend",
    "GeminiBackend",
    "HealthStatus",
]
