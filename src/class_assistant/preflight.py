"""Safety checks required before starting the class-assistant runtime."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    dll_path: str = ""
    dll_sha256: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_wcdb_dll(project_root: Path) -> Path:
    """Resolve WCDB DLL using the same candidate order as the runtime loader."""
    source_path = Path(project_root) / "native" / "windows" / "wcdb_api.dll"
    candidates = [source_path]
    if getattr(sys, "frozen", False):
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "native" / "windows" / "wcdb_api.dll")
        candidates.extend([
            Path(sys.executable).resolve().parent / "native" / "windows" / "wcdb_api.dll",
            source_path,
        ])
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[-1])


def run_preflight(
    config: Any,
    project_root: Path,
    allowed_hashes: Iterable[str] = (),
) -> PreflightReport:
    errors: list[str] = []
    raw_groups = getattr(config, "class_assistant_groups", None)
    if isinstance(raw_groups, str):
        raw_groups = raw_groups.split(",")
    groups: tuple[str, ...] = ()
    if raw_groups is None:
        errors.append("CLASS_ASSISTANT_GROUPS must contain explicit stable chat_id values")
    else:
        try:
            if not all(isinstance(group, str) for group in raw_groups):
                raise TypeError("group values must be strings")
            groups = tuple(group.strip() for group in raw_groups)
        except TypeError:
            errors.append("CLASS_ASSISTANT_GROUPS must be an iterable of strings")
    if not groups or any(not group or group == "*" for group in groups):
        errors.append(
            "CLASS_ASSISTANT_GROUPS must contain explicit stable chat_id values"
        )
    if bool(getattr(config, "class_assistant_real_send_enabled", False)) and bool(
        getattr(config, "class_assistant_dry_run", True)
    ):
        errors.append(
            "REAL_SEND_ENABLED requires an explicit post-rollout configuration with DRY_RUN=false"
        )

    dll = resolve_wcdb_dll(project_root)
    if not dll.is_file():
        errors.append("native/windows/wcdb_api.dll is missing")
        return PreflightReport(False, str(dll), "", tuple(errors))

    digest = _sha256(dll)
    allowed = set()
    invalid_hash = False
    try:
        for value in allowed_hashes:
            if not isinstance(value, str):
                invalid_hash = True
                continue
            normalized = value.strip().lower()
            if normalized:
                allowed.add(normalized)
    except TypeError:
        invalid_hash = True
    if invalid_hash:
        errors.append("WCDB_ALLOWED_SHA256 must contain only string hashes")
    if not allowed:
        errors.append("WCDB_ALLOWED_SHA256 must contain at least one reviewed hash")
    elif digest.lower() not in allowed:
        errors.append("wcdb_api.dll SHA-256 is not in the reviewed allowlist")
    return PreflightReport(not errors, str(dll), digest, tuple(errors))
