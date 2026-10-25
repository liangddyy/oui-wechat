"""Prepare an isolated runtime directory; never accept credentials."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, load_config

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error("Supply a base URL without credentials or query parameters")

    runtime = ROOT / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    config_path = runtime / "config.yaml"
    source = config_path if config_path.exists() else EXAMPLE_CONFIG_PATH
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    config["owui"]["base_url"] = args.url.rstrip("/")
    if not config_path.exists():
        config["capabilities"]["allow_terminal"] = False
        config["capabilities"]["image_generation"] = False
    descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        yaml.safe_dump(config, file, allow_unicode=True, sort_keys=False)
    config_path.chmod(0o600)
    load_config(config_path)

    env_path = ROOT / ".env"
    if not env_path.exists():
        descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as file:
            file.write(f"LOCAL_UID={os.getuid()}\nLOCAL_GID={os.getgid()}\n")
    print(f"Runtime configuration: {config_path}")
    print("Only the Open WebUI base URL was configured; no account credentials were requested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
