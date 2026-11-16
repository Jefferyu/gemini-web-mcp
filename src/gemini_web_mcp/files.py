"""Security and size boundary validation for local file uploads."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

_SENSITIVE_DIR_BASES = (
    Path("~/.ssh"),
    Path("~/.aws"),
    Path("~/.gnupg"),
)


class FileValidationError(ValueError):
    """Raised when an upload file fails existence, size, or security checks."""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_sensitive_env_name(name: str) -> bool:
    if name == ".env":
        return True
    return name.startswith(".env.") and name != ".env.example"


def validate_upload_files(
    files: Sequence[str | Path],
    *,
    max_count: int = 10,
    max_size_mb: int = 20,
    extra_blocked_paths: Sequence[Path] = (),
) -> list[Path]:
    """Validate local files before uploading them to Gemini Web."""
    if len(files) > max_count:
        raise FileValidationError(
            f"Too many files ({len(files)}). Maximum allowed per request is {max_count}."
        )

    blocked_dirs = [base.expanduser().resolve() for base in _SENSITIVE_DIR_BASES]
    blocked_files = {Path(p).expanduser().resolve() for p in extra_blocked_paths}
    max_bytes = max_size_mb * 1024 * 1024

    validated: list[Path] = []
    for raw_item in files:
        raw_path = Path(raw_item).expanduser()
        resolved = raw_path.resolve()

        if any(_is_relative_to(resolved, blocked_dir) for blocked_dir in blocked_dirs):
            raise FileValidationError(
                f"Refusing to upload sensitive path inside restricted directory: {raw_item}"
            )

        if _is_sensitive_env_name(resolved.name) or _is_sensitive_env_name(raw_path.name):
            raise FileValidationError(
                f"Refusing to upload environment secret file: {raw_item}"
            )

        if resolved in blocked_files:
            raise FileValidationError(
                f"Refusing to upload protected application file: {raw_item}"
            )

        if not resolved.exists():
            raise FileValidationError(f"File does not exist: {raw_item}")

        if resolved.is_dir():
            raise FileValidationError(f"Path is a directory, not a regular file: {raw_item}")

        if not resolved.is_file():
            raise FileValidationError(f"Path is not a regular file: {raw_item}")

        size_bytes = resolved.stat().st_size
        if size_bytes > max_bytes:
            size_mb = size_bytes / (1024 * 1024)
            raise FileValidationError(
                f"File {raw_item} is {size_mb:.2f} MB, exceeding the {max_size_mb} MB limit."
            )

        validated.append(resolved)

    return validated
