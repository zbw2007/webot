"""Safety checks required before starting the class-assistant runtime."""

from __future__ import annotations

import hashlib
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


def run_preflight(
    config: Any,
    project_root: Path,
    allowed_hashes: Iterable[str] = (),
) -> PreflightReport:
    errors: list[str] = []
    groups = tuple(getattr(config, "class_assistant_groups", ()) or ())
    if not groups or "*" in groups:
        errors.append(
            "CLASS_ASSISTANT_GROUPS must contain explicit stable chat_id values"
        )
    if bool(getattr(config, "class_assistant_real_send_enabled", False)) and bool(
        getattr(config, "class_assistant_dry_run", True)
    ):
        errors.append(
            "REAL_SEND_ENABLED requires an explicit post-rollout configuration with DRY_RUN=false"
        )

    dll = Path(project_root) / "native" / "windows" / "wcdb_api.dll"
    if not dll.is_file():
        errors.append("native/windows/wcdb_api.dll is missing")
        return PreflightReport(False, str(dll), "", tuple(errors))

    digest = _sha256(dll)
    allowed = {value.strip().lower() for value in allowed_hashes if value.strip()}
    if not allowed:
        errors.append("WCDB_ALLOWED_SHA256 must contain at least one reviewed hash")
    elif digest.lower() not in allowed:
        errors.append("wcdb_api.dll SHA-256 is not in the reviewed allowlist")
    return PreflightReport(not errors, str(dll), digest, tuple(errors))
