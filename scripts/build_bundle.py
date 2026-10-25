"""Create an allowlisted source bundle without local credentials or data."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

import yaml

from oui_wechat.config import validate_url
from oui_wechat.provenance import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "oui-wechat.tar.gz")
    parser.add_argument("--validation-report", type=Path, required=True)
    args = parser.parse_args()
    fingerprint = source_fingerprint(ROOT)
    report = json.loads(args.validation_report.read_text())
    if (
        report.get("source_sha256") != fingerprint
        or not report.get("source_unchanged_during_validation")
        or any(item["exit_code"] for item in report["results"].values())
    ):
        raise SystemExit("Run passing validation against the current sources before packaging")
    names = (
        "Dockerfile", "compose.yaml", ".dockerignore", "requirements.lock", "uv.lock",
        "pyproject.toml", "README.md", "DEPLOY.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "MANIFEST.in",
        ".gitignore", ".env.example",
    )
    sources = [ROOT / name for name in names]
    for directory in ("oui_wechat", "vendor"):
        sources.extend(
            path for path in (ROOT / directory).rglob("*")
            if path.is_file() and not any(
                part in {"__pycache__", ".ruff_cache", ".git"} or part.endswith(".egg-info")
                for part in path.parts
            ) and path.suffix not in {".pyc", ".pyo"}
        )
    payloads = {str(path.relative_to(ROOT)): path.read_bytes() for path in sorted(sources)}
    if args.url:
        from open_webui_weixin.config import EXAMPLE_CONFIG_PATH

        config = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text())
        config["owui"]["base_url"] = validate_url(args.url)
        config["display"]["reasoning"] = {"enable": True, "detailed": False}
        config["capabilities"]["allow_terminal"] = False
        config["capabilities"]["image_generation"] = False
        payloads["runtime/config.yaml"] = yaml.safe_dump(
            config, allow_unicode=True, sort_keys=False
        ).encode()
    manifest = {
        "source_sha256": fingerprint,
        "contains_credentials": False,
        "file_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
    }
    payloads["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        for name, data in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600 if name.startswith("runtime/") else 0o644
            archive.addfile(info, io.BytesIO(data))
    print(f"Bundle: {args.output}")
    print(f"SHA256: {hashlib.sha256(args.output.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
