"""Cookie-based Gemini Web backend powered by gemini-webapi 2.x."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from gemini_webapi import GeminiClient
from gemini_webapi.exceptions import (
    APIError,
    AuthError,
    GeminiError,
    ModelInvalidError,
    TemporarilyBlockedError,
    UsageLimitExceededError,
)
from gemini_webapi.exceptions import TimeoutError as GeminiTimeoutError

from gemini_web_mcp.auth import (
    AuthConfigurationError,
    AuthCredentials,
    AuthLoader,
    clear_rotated_cache,
)
from gemini_web_mcp.backends.base import (
    AskRequest,
    AskResponse,
    BackendError,
    GeminiBackend,
    HealthStatus,
)
from gemini_web_mcp.config import Settings, mask_proxy

_CANONICAL_MODEL_ALIASES: dict[str, str] = {
    "": "unspecified",
    "default": "unspecified",
    "unspecified": "unspecified",
    "flash": "gemini-flash",
    "gemini-flash": "gemini-flash",
    "pro": "gemini-pro",
    "gemini-pro": "gemini-pro",
    "thinking": "thinking",
    "gemini-thinking": "thinking",
}


def _default_client_factory(
    secure_1psid: str,
    secure_1psidts: str | None,
    proxy: str | None,
) -> Any:
    return GeminiClient(
        secure_1psid=secure_1psid,
        secure_1psidts=secure_1psidts,
        proxy=proxy,
    )


class CookieBackend(GeminiBackend):
    """GeminiBackend implementation using browser cookies and gemini-webapi."""

    def __init__(
        self,
        settings: Settings,
        *,
        auth_loader: AuthLoader | None = None,
        client_factory: Callable[[str, str | None, str | None], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._auth_loader = auth_loader or AuthLoader(cookies_path=settings.cookies_path)
        self._client_factory = client_factory or _default_client_factory
        self._init_lock = asyncio.Lock()
        self._client: Any | None = None
        self._credentials: AuthCredentials | None = None
        self._initialized = False

    async def init(self) -> None:
        """Lazily initialize GeminiClient once using double-checked locking."""
        if self._initialized and self._client is not None:
            return

        async with self._init_lock:
            if self._initialized and self._client is not None:
                return

            try:
                creds = self._auth_loader.load(prefer_rotated_cache=True)
            except AuthConfigurationError as exc:
                raise BackendError(str(exc)) from exc

            try:
                client = await self._init_client(creds)
            except AuthError as first_err:
                if "rotated-cache" in creds.source:
                    clear_rotated_cache(creds.secure_1psid)
                    try:
                        creds = self._auth_loader.load(prefer_rotated_cache=False)
                        client = await self._init_client(creds)
                    except AuthConfigurationError as cfg_err:
                        raise BackendError(str(cfg_err)) from cfg_err
                    except Exception as retry_err:
                        raise self._translate_exception(retry_err, context="init") from retry_err
                else:
                    raise self._translate_exception(first_err, context="init") from first_err
            except Exception as exc:
                raise self._translate_exception(exc, context="init") from exc

            self._credentials = creds
            self._client = client
            self._initialized = True

    async def _init_client(self, creds: AuthCredentials) -> Any:
        client = self._client_factory(
            creds.secure_1psid,
            creds.secure_1psidts,
            self._settings.proxy,
        )
        try:
            await client.init(
                timeout=float(self._settings.timeout_seconds),
                auto_close=False,
                auto_refresh=True,
                verbose=False,
            )
        except Exception:
            await self._safe_close_client(client)
            raise
        return client

    @staticmethod
    async def _safe_close_client(client: Any) -> None:
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.close()

    def normalize_model(self, model: str | None) -> str:
        """Resolve aliases consistently against active client models when available."""
        if model is None:
            return "unspecified"
        raw = model.strip()
        lowered = raw.lower()
        if not lowered or lowered in ("default", "unspecified"):
            return "unspecified"

        if self._initialized and self._client is not None:
            try:
                resolved = self._client.resolve_model(raw)
                resolved_name = str(getattr(resolved, "model_name", "") or resolved).strip()
                if resolved_name:
                    return resolved_name.lower()
            except ValueError:
                pass
            except Exception:
                pass

        return _CANONICAL_MODEL_ALIASES.get(lowered, lowered)

    def _resolve_model_and_thinking(
        self, requested_model: str | None
    ) -> tuple[Any, str, bool]:
        """Map requested model string to (client_model_arg, resolved_name, extended_thinking)."""
        assert self._client is not None
        raw = (requested_model or self._settings.default_model or "unspecified").strip()
        lowered = raw.lower()

        if not lowered or lowered in ("default", "unspecified"):
            return None, "unspecified", False

        is_thinking = "thinking" in lowered
        try:
            resolved = self._client.resolve_model(raw)
            resolved_name = str(getattr(resolved, "model_name", "") or resolved).strip() or raw
            return resolved, resolved_name, is_thinking
        except ValueError as exc:
            if lowered == "thinking":
                return None, "thinking", True
            available = self._get_available_model_names()
            available_hint = ", ".join(available) if available else "none discovered"
            raise BackendError(
                f"Unknown Gemini model '{raw}'. Available models: {available_hint}. "
                "You can also pass 'unspecified', 'flash', 'pro', or 'thinking'."
            ) from exc

    def _get_available_model_names(self) -> list[str]:
        if self._client is None:
            return []
        try:
            models = self._client.list_models()
        except Exception:
            return []
        if not models:
            return []
        names: list[str] = []
        for item in models:
            name = getattr(item, "model_name", None) or str(item)
            if name:
                names.append(str(name))
        return names

    async def ask(self, req: AskRequest) -> AskResponse:
        """Send a text/multimodal prompt to Gemini Web."""
        await self.init()
        assert self._client is not None

        model_arg, resolved_name, is_thinking = self._resolve_model_and_thinking(req.model)
        effective_timeout = req.timeout_seconds or (
            self._settings.thinking_timeout_seconds
            if is_thinking
            else self._settings.timeout_seconds
        )

        chat_kwargs: dict[str, Any] = {}
        if req.metadata:
            chat_kwargs["metadata"] = req.metadata
        if model_arg is not None:
            chat_kwargs["model"] = model_arg

        chat = self._client.start_chat(**chat_kwargs)
        send_kwargs: dict[str, Any] = {
            "files": req.files if req.files else None,
            "extended_thinking": is_thinking,
            "timeout": float(effective_timeout),
        }

        try:
            output = await chat.send_message(req.prompt, **send_kwargs)
        except Exception as exc:
            raise self._translate_exception(exc, context="ask") from exc

        raw_metadata = getattr(output, "metadata", None) or getattr(chat, "metadata", None) or []
        metadata: list[str | None] = [
            None if item is None else str(item) for item in raw_metadata
        ]
        thoughts_raw = getattr(output, "thoughts", None)
        thoughts = str(thoughts_raw) if (req.include_thoughts and thoughts_raw) else None

        return AskResponse(
            text=str(getattr(output, "text", "") or ""),
            thoughts=thoughts,
            metadata=metadata,
            resolved_model=resolved_name,
        )

    async def health(self) -> HealthStatus:
        """Verify initialization, auth source, proxy masking, and available models."""
        proxy_masked = mask_proxy(self._settings.proxy)
        try:
            await self.init()
        except BackendError as exc:
            return HealthStatus(
                ok=False,
                backend="cookie",
                auth_source=self._credentials.source if self._credentials else None,
                proxy_configured=self._settings.proxy is not None,
                proxy_masked=proxy_masked,
                available_models=[],
                message=str(exc),
            )

        models = self._get_available_model_names()
        return HealthStatus(
            ok=True,
            backend="cookie",
            auth_source=self._credentials.source if self._credentials else None,
            proxy_configured=self._settings.proxy is not None,
            proxy_masked=proxy_masked,
            available_models=models,
            message="Gemini Web CookieBackend is authenticated and ready.",
        )

    async def close(self) -> None:
        """Close the active GeminiClient and stop background refresh tasks."""
        async with self._init_lock:
            client = self._client
            self._client = None
            self._initialized = False
            await self._safe_close_client(client)

    def _translate_exception(self, exc: Exception, *, context: str) -> BackendError:
        if isinstance(exc, BackendError):
            return exc

        proxy_hint = (
            f"Active proxy: {mask_proxy(self._settings.proxy)}."
            if self._settings.proxy
            else "No proxy configured; on macOS GUI clients set GEMINI_WEB_MCP_PROXY if needed."
        )

        if isinstance(exc, AuthError):
            return BackendError(
                "Gemini Web authentication failed (cookies expired or invalid). "
                f"Update {self._settings.cookies_path} or GEMINI_SECURE_1PSID / "
                "GEMINI_SECURE_1PSIDTS and retry."
            )

        if isinstance(exc, TemporarilyBlockedError):
            return BackendError(
                f"Gemini Web temporarily blocked or rate-limited this request: {exc}"
            )

        if isinstance(exc, UsageLimitExceededError):
            return BackendError(
                f"Gemini Web usage limit exceeded for the selected model: {exc}"
            )

        if isinstance(exc, ModelInvalidError):
            available = ", ".join(self._get_available_model_names()) or "none"
            return BackendError(
                f"Selected model is invalid or unavailable ({exc}). Available: {available}."
            )

        if isinstance(exc, (GeminiTimeoutError, TimeoutError, asyncio.TimeoutError)):
            return BackendError(
                f"Gemini Web request timed out during {context}. "
                f"For complex prompts consider model='thinking' or increasing timeout. {proxy_hint}"
            )

        if isinstance(exc, (APIError, GeminiError)):
            return BackendError(f"Gemini Web API error during {context}: {exc}")

        if isinstance(exc, OSError) or "curl" in type(exc).__name__.lower():
            return BackendError(
                f"Network/proxy error connecting to gemini.google.com ({type(exc).__name__}: "
                f"{exc}). {proxy_hint}"
            )

        return BackendError(
            f"Unexpected Gemini backend error during {context} ({type(exc).__name__}): {exc}"
        )
