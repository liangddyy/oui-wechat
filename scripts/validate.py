"""Run the pinned upstream suites and retain their actual exit statuses."""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

from oui_wechat.provenance import source_fingerprint

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "vendor" / "open-webui-weixin"
SUITES = ("caps", "local", "chat", "integration", "queue", "temp", "typing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.label):
        parser.error("label must contain only letters, digits, underscores or hyphens")
    report_dir = ROOT / "reports" / args.label
    report_dir.mkdir(parents=True, exist_ok=True)
    revision = json.loads((UPSTREAM / "UPSTREAM.json").read_text())["commit"]
    fingerprint = source_fingerprint(ROOT)
    commands = {
        **{name: [sys.executable, "-B", f"tests/test_{name}.py"] for name in SUITES},
        "contracts": [
            sys.executable, "-B", str(ROOT / "validation" / "test_contracts.py")
        ],
        "web": [
            sys.executable, "-B", str(ROOT / "tests" / "test_web.py")
        ],
        "lint": [
            sys.executable, "-m", "ruff", "check", "--no-cache", "."
        ],
    }
    results = {}
    for name, command in commands.items():
        try:
            process = subprocess.run(
                command, cwd=UPSTREAM, capture_output=True, text=True, timeout=90,
                check=False,
            )
            output = process.stdout + process.stderr
            exit_code = process.returncode
        except subprocess.TimeoutExpired as exc:
            output = f"Timed out after {exc.timeout} seconds"
            exit_code = 124
        (report_dir / f"{name}.log").write_text(output, encoding="utf-8")
        results[name] = {
            "exit_code": exit_code,
            "passed_assertions": len(re.findall(r"^\s+PASS\s", output, re.MULTILINE)),
            "failed_assertions": re.findall(
                r"^\s+FAIL\s+(.+)$", output, re.MULTILINE
            ),
            "unittest_cases": sum(int(n) for n in re.findall(r"Ran (\d+) tests?", output)),
        }
        print(
            f"{name}: {'PASS' if exit_code == 0 else 'FAIL'} "
            f"(exit={exit_code}, assertions_passed={results[name]['passed_assertions']})",
            flush=True,
        )
    report = {
        "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "revision": revision,
        "upstream_modified": True,
        "source_sha256": fingerprint,
        "source_unchanged_during_validation": source_fingerprint(ROOT) == fingerprint,
        "python": sys.version,
        "scope": "Local tests; no real Open WebUI or authenticated WeChat traffic",
        "results": results,
    }
    (report_dir / "summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return int(any(result["exit_code"] for result in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
