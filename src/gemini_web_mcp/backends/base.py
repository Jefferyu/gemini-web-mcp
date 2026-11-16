"""Abstract backend interface and unified request/response models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field


class BackendError(RuntimeError):
    """Actionable, user-facing error raised by a Gemini backend."""


class AskRequest(BaseModel):
    """Unified request payload sent to a GeminiBackend."""

    prompt: str
    metadata: list[str | None] | None = None
    model: str | None = None
    files: list[Path] = Field(default_factory=list)
    include_thoughts: bool = False
    timeout_seconds: int | None = None


class AskResponse(BaseModel):
    """Unified response returned by a GeminiBackend."""

    text: str
    thoughts: str | None = None
    metadata: list[str | None] = Field(default_factory=list)
    resolved_model: str = "unspecified"


class HealthStatus(BaseModel):
    """Backend health and diagnostic summary."""

    ok: bool
    backend: str
    auth_source: str | None = None
    proxy_configured: bool = False
    proxy_masked: str | None = None
    available_models: list[str] = Field(default_factory=list)
    message: str = ""


class GeminiBackend(ABC):
    """Pluggable abstract base class for Gemini Web backends."""

    @abstractmethod
    async def init(self) -> None:
        """Initialize backend resources and authenticate if needed."""

    @abstractmethod
    async def ask(self, req: AskRequest) -> AskResponse:
        """Send a prompt (optionally with conversation metadata and files)."""

    @abstractmethod
    async def health(self) -> HealthStatus:
        """Check backend authentication, proxy configuration, and available models."""

    @abstractmethod
    async def close(self) -> None:
        """Release underlying connections and background tasks."""

    def normalize_model(self, model: str | None) -> str:
        """Normalize a model name or alias for session-consistency checks."""
        if model is None:
            return "unspecified"
        cleaned = model.strip().lower()
        return cleaned or "unspecified"
