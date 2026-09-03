import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.class_assistant.preflight import PreflightReport, run_preflight


def config(**overrides):
    values = {
        "class_assistant_enabled": True,
        "class_assistant_groups": ["class@chatroom"],
        "class_assistant_real_send_enabled": False,
        "class_assistant_dry_run": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def put_dll(root: Path, content: bytes = b"test dll") -> str:
    path = root / "native" / "windows" / "wcdb_api.dll"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_missing_dll_is_reported(tmp_path):
    report = run_preflight(config(), tmp_path, allowed_hashes=["a" * 64])
    assert isinstance(report, PreflightReport)
    assert report.ok is False
    assert any("dll" in error.lower() for error in report.errors)


def test_empty_hash_allowlist_is_reported(tmp_path):
    put_dll(tmp_path)
    report = run_preflight(config(), tmp_path, allowed_hashes=())
    assert report.ok is False
    assert any("wcdb_allowed_sha256" in error.lower() for error in report.errors)


def test_missing_explicit_group_is_reported(tmp_path):
    put_dll(tmp_path)
    report = run_preflight(config(class_assistant_groups=[]), tmp_path, allowed_hashes=["a" * 64])
    assert report.ok is False
    assert any("group" in error.lower() for error in report.errors)


def test_wildcard_group_is_reported(tmp_path):
    put_dll(tmp_path)
    report = run_preflight(config(class_assistant_groups=["*"]), tmp_path, allowed_hashes=["a" * 64])
    assert report.ok is False
    assert any("group" in error.lower() for error in report.errors)


def test_real_send_and_dry_run_conflict_is_reported(tmp_path):
    put_dll(tmp_path)
    report = run_preflight(
        config(class_assistant_real_send_enabled=True, class_assistant_dry_run=True),
        tmp_path,
        allowed_hashes=["a" * 64],
    )
    assert report.ok is False
    assert any("DRY_RUN" in error for error in report.errors)


def test_approved_hash_passes(tmp_path):
    digest = put_dll(tmp_path, b"approved test dll")
    report = run_preflight(config(), tmp_path, allowed_hashes=[digest])
    assert report.ok is True
    assert report.dll_sha256 == digest
    assert Path(report.dll_path) == tmp_path / "native" / "windows" / "wcdb_api.dll"
    assert report.errors == ()
