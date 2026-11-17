"""Pluggable Gemini backend implementations."""

from gemini_web_mcp.backends.base import (
    AskRequest,
    AskResponse,
    BackendError,
    GeminiBackend,
    HealthStatus,
)
from gemini_web_mcp.backends.cookie_backend import CookieBackend
from gemini_web_mcp.config import Settings


def create_backend(settings: Settings) -> GeminiBackend:
    """Instantiate the configured GeminiBackend implementation."""
    backend_name = settings.backend.strip().lower()
    if backend_name == "cookie":
        return CookieBackend(settings)
    raise ValueError(
        f"Unsupported backend '{settings.backend}'. Currently supported backends: 'cookie'."
    )


__all__ = [
    "AskRequest",
    "AskResponse",
    "BackendError",
    "CookieBackend",
    "GeminiBackend",
    "HealthStatus",
    "create_backend",
]
