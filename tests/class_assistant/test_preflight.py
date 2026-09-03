import hashlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.class_assistant.preflight import (
    PreflightReport,
    resolve_wcdb_dll,
    run_preflight,
)
from src.wechat.wcdb_paths import wcdb_dll_candidates


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


def test_whitespace_wildcard_group_is_reported(tmp_path):
    put_dll(tmp_path)
    report = run_preflight(
        config(class_assistant_groups=[" * "]),
        tmp_path,
        allowed_hashes=["a" * 64],
    )
    assert report.ok is False
    assert any("group" in error.lower() for error in report.errors)


def test_hash_mismatch_is_reported(tmp_path):
    put_dll(tmp_path, b"unreviewed dll")
    report = run_preflight(config(), tmp_path, allowed_hashes=["a" * 64])
    assert report.ok is False
    assert any("sha-256" in error.lower() for error in report.errors)


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


def test_frozen_dll_resolution_matches_loader_priority(tmp_path, monkeypatch):
    meipass = tmp_path / "meipass"
    exe_dir = tmp_path / "exe"
    source_root = tmp_path / "source"
    for root in (meipass, exe_dir, source_root):
        (root / "native" / "windows").mkdir(parents=True)
    meipass_dll = meipass / "native" / "windows" / "wcdb_api.dll"
    exe_dll = exe_dir / "native" / "windows" / "wcdb_api.dll"
    source_dll = source_root / "native" / "windows" / "wcdb_api.dll"
    exe_dll.write_bytes(b"exe")
    source_dll.write_bytes(b"source")
    monkeypatch.setattr("src.wechat.wcdb_paths.sys.frozen", True, raising=False)
    monkeypatch.setattr("src.wechat.wcdb_paths.sys._MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr("src.wechat.wcdb_paths.sys.executable", str(exe_dir / "bot.exe"))
    assert resolve_wcdb_dll(source_root) == exe_dll
    meipass_dll.write_bytes(b"meipass")
    assert resolve_wcdb_dll(source_root) == meipass_dll


def test_frozen_resolution_without_meipass_does_not_raise(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    dll = source_root / "native" / "windows" / "wcdb_api.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"source")
    monkeypatch.setattr("src.wechat.wcdb_paths.sys.frozen", True, raising=False)
    monkeypatch.delattr("src.wechat.wcdb_paths.sys._MEIPASS", raising=False)
    monkeypatch.setattr("src.wechat.wcdb_paths.sys.executable", str(tmp_path / "bot.exe"))
    assert resolve_wcdb_dll(source_root) == dll


@pytest.mark.parametrize("raw_groups", [None, 123, [None], [123]])
def test_non_string_groups_are_rejected(tmp_path, raw_groups):
    digest = put_dll(tmp_path)
    report = run_preflight(config(class_assistant_groups=raw_groups), tmp_path, [digest])
    assert report.ok is False
    assert any("group" in error.lower() for error in report.errors)


def test_generator_groups_are_materialized_once_and_accepted(tmp_path):
    digest = put_dll(tmp_path)
    report = run_preflight(
        config(class_assistant_groups=(group for group in ["class@chatroom"])),
        tmp_path,
        [digest],
    )
    assert report.ok is True


def test_generator_groups_with_invalid_item_are_rejected(tmp_path):
    digest = put_dll(tmp_path)
    report = run_preflight(
        config(class_assistant_groups=(group for group in ["class@chatroom", 123])),
        tmp_path,
        [digest],
    )
    assert report.ok is False
    assert any("group" in error.lower() for error in report.errors)


def test_shared_wcdb_resolver_uses_same_candidates_as_preflight(tmp_path, monkeypatch):
    meipass = tmp_path / "meipass"
    exe_dir = tmp_path / "exe"
    source_root = tmp_path / "source"
    monkeypatch.setattr("src.wechat.wcdb_paths.sys.frozen", True, raising=False)
    monkeypatch.setattr("src.wechat.wcdb_paths.sys._MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr("src.wechat.wcdb_paths.sys.executable", str(exe_dir / "bot.exe"))
    expected = (
        meipass / "native" / "windows" / "wcdb_api.dll",
        exe_dir / "native" / "windows" / "wcdb_api.dll",
        source_root / "native" / "windows" / "wcdb_api.dll",
    )
    assert wcdb_dll_candidates(source_root) == expected
    assert resolve_wcdb_dll(source_root) == expected[-1]


def test_runtime_loader_uses_shared_wcdb_resolver_priority(tmp_path, monkeypatch):
    from src.wechat.wcdb_client import _find_dll

    expected = tmp_path / "native" / "windows" / "wcdb_api.dll"
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"exe")
    seen = {}

    def resolve(source_root):
        seen["source_root"] = source_root
        return expected

    monkeypatch.setattr("src.wechat.wcdb_client.resolve_wcdb_dll", resolve)
    assert _find_dll() == (str(expected.parent), str(expected))
    assert seen["source_root"] == Path(__file__).resolve().parents[2]


def test_non_string_hash_allowlist_is_rejected_without_crashing(tmp_path):
    put_dll(tmp_path)
    report = run_preflight(config(), tmp_path, [None, 123])
    assert report.ok is False
    assert any("sha256" in error.lower() for error in report.errors)


def test_directory_named_dll_is_not_read(tmp_path):
    dll = tmp_path / "native" / "windows" / "wcdb_api.dll"
    dll.mkdir(parents=True)
    report = run_preflight(config(), tmp_path, ["a" * 64])
    assert report.ok is False
    assert report.dll_sha256 == ""


def test_preflight_uses_loader_source_root_when_app_home_differs(monkeypatch, tmp_path):
    try:
        from src.bot import Bot
    except ModuleNotFoundError as exc:
        if exc.name != "anthropic":
            raise
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
        fake_anthropic.APIConnectionError = type("APIConnectionError", (Exception,), {})
        fake_anthropic.InternalServerError = type("InternalServerError", (Exception,), {})
        fake_anthropic.Anthropic = object
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
        from src.bot import Bot
    bot = Bot.__new__(Bot)
    bot._config = config()
    seen = {}
    monkeypatch.setenv("WEBOT_APP_HOME", str(tmp_path / "app-home"))
    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path / "patched-project-root")
    monkeypatch.setattr(
        "src.class_assistant.preflight.run_preflight",
        lambda config, root, allowed_hashes: seen.update(root=root) or PreflightReport(False, errors=("blocked",)),
    )
    with pytest.raises(RuntimeError):
        bot._create_checked_wechat_backend(None)
    assert seen["root"] == Path(__file__).resolve().parents[2]


def test_preflight_does_not_load_dll(tmp_path, monkeypatch):
    digest = put_dll(tmp_path)
    def fail_load(*args, **kwargs):
        raise AssertionError("preflight must not load DLL")
    monkeypatch.setattr("ctypes.CDLL", fail_load)
    report = run_preflight(config(), tmp_path, allowed_hashes=[digest])
    assert report.ok is True


def test_bot_preflight_failure_prevents_backend_creation(monkeypatch):
    try:
        from src.bot import Bot
    except ModuleNotFoundError as exc:
        if exc.name != "anthropic":
            raise
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
        fake_anthropic.APIConnectionError = type("APIConnectionError", (Exception,), {})
        fake_anthropic.InternalServerError = type("InternalServerError", (Exception,), {})
        fake_anthropic.Anthropic = object
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
        from src.bot import Bot
    bot = Bot.__new__(Bot)
    bot._config = config()
    created = 0

    def create_backend(store=None):
        nonlocal created
        created += 1
        return object()

    monkeypatch.setattr("src.bot.PROJECT_ROOT", Path("/project"))
    monkeypatch.setattr(
        "src.class_assistant.preflight.run_preflight",
        lambda *args, **kwargs: PreflightReport(False, errors=("blocked",)),
    )
    bot._create_wechat_backend = create_backend
    with pytest.raises(RuntimeError, match="preflight failed"):
        bot._create_checked_wechat_backend(None)
    assert created == 0
