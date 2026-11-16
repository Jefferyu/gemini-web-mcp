"""Tests for upload file validation and sensitive path blocking."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gemini_web_mcp.files import FileValidationError, validate_upload_files


def test_validate_upload_files_success(tmp_path: Path) -> None:
    sample = tmp_path / "notes.md"
    sample.write_text("# Hello\n", encoding="utf-8")
    env_example = tmp_path / ".env.example"
    env_example.write_text("KEY=placeholder\n", encoding="utf-8")

    result = validate_upload_files([sample, env_example])
    assert result == [sample.resolve(), env_example.resolve()]


def test_validate_upload_files_count_and_size_limits(tmp_path: Path) -> None:
    files = []
    for i in range(4):
        f = tmp_path / f"f{i}.txt"
        f.write_text("ok", encoding="utf-8")
        files.append(f)

    with pytest.raises(FileValidationError, match="Too many files"):
        validate_upload_files(files, max_count=3)

    big_file = tmp_path / "large.bin"
    big_file.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(FileValidationError, match="exceeding the 1 MB limit"):
        validate_upload_files([big_file], max_size_mb=1)


def test_validate_upload_files_rejects_missing_and_dir(tmp_path: Path) -> None:
    with pytest.raises(FileValidationError, match="does not exist"):
        validate_upload_files([tmp_path / "missing.txt"])

    subdir = tmp_path / "folder"
    subdir.mkdir()
    with pytest.raises(FileValidationError, match="directory"):
        validate_upload_files([subdir])


def test_validate_upload_files_blocks_sensitive_paths_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    id_rsa = ssh_dir / "id_rsa"
    id_rsa.write_text("SECRET_KEY", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))

    # Symlink pointing to ~/.ssh/id_rsa
    symlink_path = tmp_path / "innocent.txt"
    os.symlink(id_rsa, symlink_path)

    with pytest.raises(FileValidationError, match="restricted directory"):
        validate_upload_files([symlink_path])

    # .env and .env.local
    dotenv = tmp_path / ".env"
    dotenv.write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(FileValidationError, match="environment secret"):
        validate_upload_files([dotenv])

    dotenv_local = tmp_path / ".env.local"
    dotenv_local.write_text("SECRET=2", encoding="utf-8")
    with pytest.raises(FileValidationError, match="environment secret"):
        validate_upload_files([dotenv_local])

    # Protected extra files (cookies.json, sessions.db)
    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text("{}", encoding="utf-8")
    cookies_link = tmp_path / "alias.json"
    os.symlink(cookies_file, cookies_link)
    with pytest.raises(FileValidationError, match="protected application file"):
        validate_upload_files([cookies_link], extra_blocked_paths=[cookies_file])
