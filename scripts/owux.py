"""Run OWUX using this workspace's isolated runtime and Python environment."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"

if __name__ == "__main__":
    if not (RUNTIME / "config.yaml").is_file():
        raise SystemExit("Run scripts/configure.py --url <Open-WebUI-base-URL> first.")
    os.umask(0o077)
    os.execv(
        sys.executable,
        [
            sys.executable, "-B", "-m", "open_webui_weixin",
            "--dir", str(RUNTIME), *sys.argv[1:],
        ],
    )
