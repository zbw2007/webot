"""Side-effect-free WCDB DLL candidate resolution."""

from __future__ import annotations

import sys
from pathlib import Path


_DLL_RELATIVE_PATH = Path("native") / "windows" / "wcdb_api.dll"


def wcdb_dll_candidates(source_root: Path) -> tuple[Path, ...]:
    """Return WCDB DLL candidates in the runtime loader's priority order."""
    source_path = Path(source_root) / _DLL_RELATIVE_PATH
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / _DLL_RELATIVE_PATH)
        executable = getattr(sys, "executable", None)
        if executable:
            candidates.append(Path(executable).resolve().parent / _DLL_RELATIVE_PATH)
    candidates.append(source_path)

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def resolve_wcdb_dll(source_root: Path) -> Path:
    """Return the first existing WCDB DLL, or the final fallback path."""
    candidates = wcdb_dll_candidates(source_root)
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[-1])
