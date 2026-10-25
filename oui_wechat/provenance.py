"""Fingerprint publishable runtime sources and regression tests."""

from __future__ import annotations

import hashlib
from pathlib import Path


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    paths = []
    for name in ("oui_wechat", "vendor", "tests", "validation"):
        paths.extend(
            path for path in (root / name).rglob("*")
            if path.is_file()
            and not any(part in {"__pycache__", ".ruff_cache", ".git"}
                        or part.endswith(".egg-info") for part in path.parts)
            and path.suffix not in {".pyc", ".pyo"}
        )
    paths.extend(root / name for name in ("pyproject.toml", "uv.lock", "requirements.lock"))
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
