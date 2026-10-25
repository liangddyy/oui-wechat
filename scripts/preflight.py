"""Probe public endpoints without credentials, messages or persistent state."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, load_config
from open_webui_weixin.main import build_client
from open_webui_weixin.weixin_protocol import IlinkError


async def probe(url: str | None, weixin: bool) -> dict:
    report = {}
    if url:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use an http(s) base URL without credentials or query parameters")
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            for endpoint in ("/health", "/api/version", "/api/config"):
                try:
                    response = await client.get(url.rstrip("/") + endpoint)
                    item = {"http_status": response.status_code}
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                    if isinstance(data, dict):
                        item["version"] = data.get("version")
                        if endpoint == "/api/config":
                            features = data.get("features") or {}
                            item["password_auth"] = features.get("enable_password_auth")
                            item["login_form"] = features.get("enable_login_form")
                    report[endpoint] = item
                except httpx.HTTPError as exc:
                    report[endpoint] = {"error_type": type(exc).__name__}
    if weixin:
        cfg = load_config(EXAMPLE_CONFIG_PATH)
        client = build_client(cfg)
        try:
            data = await client.get_bot_qrcode(cfg.weixin.bot_type, [])
            report["weixin"] = {
                "qr_issued": bool(data.get("qrcode") and data.get("qrcode_img_content")),
                "response_fields": sorted(data),
                "authenticated": False,
            }
            # QR contents are intentionally not printed or retained.
        except (IlinkError, httpx.HTTPError, ValueError) as exc:
            report["weixin"] = {"error_type": type(exc).__name__, "detail": str(exc)[:200]}
        finally:
            await client.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--weixin", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.url and not args.weixin:
        parser.error("Supply --url or --weixin")
    report = asyncio.run(probe(args.url, args.weixin))
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")
    failed = any(
        "error_type" in item
        or ("http_status" in item and item["http_status"] != 200)
        or ("qr_issued" in item and not item["qr_issued"])
        for item in report.values()
    )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
