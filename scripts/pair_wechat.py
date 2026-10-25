"""Pair WeChat locally, bind the supplied JWT without a password, then run OWUX."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import struct
import sys
import zlib
from pathlib import Path

import qrcode
from open_webui_weixin.config import load_config
from open_webui_weixin.login import LoginFlow
from open_webui_weixin.main import build_client, run, setup_logging
from open_webui_weixin.owui import OwuiClient
from open_webui_weixin.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
PAIRING = RUNTIME / "pairing"


def write_qr(content: str, path: Path) -> None:
    qr = qrcode.QRCode(border=4)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = 8
    size = len(matrix) * scale
    raw = b"".join(
        (b"\0" + bytes(pixel for bit in row for pixel in [0 if bit else 255] * scale)) * scale
        for row in matrix
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data)) + kind + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", size, size, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    path.chmod(0o600)


def status(state: str, **extra) -> None:
    data = {
        "state": state,
        "at": datetime.datetime.now(datetime.UTC).isoformat(),
        **extra,
    }
    (PAIRING / "status.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(data, ensure_ascii=False), flush=True)


class VisibleLoginFlow(LoginFlow):
    async def run(self) -> dict:
        attempt = 0
        while True:
            attempt += 1
            data = await self.client.get_bot_qrcode(self.bot_type, self.state.known_bot_tokens())
            path = PAIRING / f"qr-{attempt}.png"
            write_qr(str(data["qrcode_img_content"]), path)
            status("waiting_for_scan", qr_path=str(path), attempt=attempt)
            result = await self._poll(str(data["qrcode"]))
            if result:
                return result
            await asyncio.sleep(1)

    @staticmethod
    async def _ask_verify_code() -> str:
        status("verification_code_required")
        code_path = PAIRING / "verification-code"
        for _ in range(300):
            if code_path.exists():
                code = code_path.read_text(encoding="ascii").strip()
                code_path.unlink()
                if code.isdigit():
                    return code
            await asyncio.sleep(1)
        raise TimeoutError("Pairing code was not supplied")


async def main(credentials: dict) -> int:
    os.umask(0o077)
    PAIRING.mkdir(parents=True, mode=0o700, exist_ok=True)
    cfg = load_config(RUNTIME / "config.yaml")
    owui = OwuiClient(cfg.owui.base_url)
    client = build_client(cfg)
    state = StateStore(cfg.state_path)
    try:
        session = await owui.whoami(credentials["jwt_token"])
        if session.user_id != credentials["user_id"]:
            raise RuntimeError("JWT identity mismatch")
        result = await VisibleLoginFlow(client, state, cfg.weixin.bot_type).run()
        scanner = result["scanner_user_id"]
        if not scanner:
            raise RuntimeError("Missing scanner identity")
        state.upsert_binding(scanner, {
            "email": session.email, "password": "",
            "owui_user_id": session.user_id, "owui_name": session.name,
            "jwt_token": session.jwt_token, "jwt_expires_at": session.expires_at,
        })
        resolved = await owui.resolve_default_model(session.jwt_token)
        state.set_focus(
            scanner, chat_id=None, leaf_id=None, temporary=0, is_first_message=True,
            model_id=resolved[0] if resolved else None,
        )
        status("paired_and_bound", binding_mode="jwt_only", password_stored=False)
    finally:
        await client.close()
        await owui.close()
        state.close()
    setup_logging(cfg)
    status("service_running", pid=os.getpid())
    return await run(cfg)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(json.load(sys.stdin))))
