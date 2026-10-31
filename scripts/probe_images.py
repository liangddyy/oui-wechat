"""Private deployment probe. Generates its own image and removes only its own Open WebUI artifacts."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from open_webui_weixin.chat import ChatRunner
from open_webui_weixin.config import load_config
from open_webui_weixin.main import build_client
from open_webui_weixin.owui import OwuiClient
from open_webui_weixin.owui_socket import OwuiSocket
from open_webui_weixin.state import StateStore
from PIL import Image

from oui_wechat.config import MediaConfig
from oui_wechat.image_transport import OwuiImages, WeixinImages, normalize_image


class ProbeClient(OwuiClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.created_chats = set()

    async def start_chat_completion(self, token, body):
        body["background_tasks"] = {
            "title_generation": False, "tags_generation": False, "follow_up_generation": False,
        }
        response = await super().start_chat_completion(token, body)
        if isinstance(response, dict) and response.get("chat_id"):
            self.created_chats.add(response["chat_id"])
        return response


async def probe(directory: Path, *, chat: bool) -> dict:
    cfg = load_config(directory / "config.yaml")
    settings = MediaConfig()
    report = {"wechat_message_sent": False, "errors": []}
    db = sqlite3.connect(f"file:{cfg.state_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT s.*,b.jwt_token,b.owui_user_id,f.model_id FROM weixin_session s "
        "LEFT JOIN binding b ON b.wechat_user_id=s.scanner_user_id "
        "LEFT JOIN focus f ON f.wechat_user_id=s.scanner_user_id ORDER BY s.created_at"
    ).fetchall()
    db.close()
    if not rows:
        return {**report, "skipped": "no_active_accounts"}
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (255, 0, 0)).save(buffer, format="PNG")
    image = normalize_image(buffer.getvalue(), settings.max_image_bytes)
    ilink, media = build_client(cfg), WeixinImages(settings)
    ilink.base_url = rows[0]["base_url"] or cfg.weixin.base_url
    try:
        item = await media.upload(ilink, rows[0]["bot_token"], rows[0]["scanner_user_id"], image)
        report["wechat_cdn_upload"] = item.get("type") == 2 and bool(item["image_item"]["media"])
    except Exception as exc:
        report["errors"].append({"stage": "wechat_cdn_upload", "type": type(exc).__name__})
    finally:
        await ilink.close()
        await media.close()

    client = ProbeClient(cfg.owui.base_url)
    token, account, uploaded, socket = None, None, None, None
    try:
        # Only use a backend administrator for this explicitly isolated probe, never a friend's user account.
        for row in rows:
            if not row["jwt_token"]:
                continue
            try:
                identity = await client.whoami(row["jwt_token"])
                if identity.role == "admin" and identity.user_id == row["owui_user_id"]:
                    token, account = row["jwt_token"], row
                    break
            except Exception:
                continue
        if not account:
            report["openwebui_skipped"] = "no_backend_admin_binding"
            return report
        access = OwuiImages(client, settings)
        uploaded = await access.upload(token, image, "oui-image-integration-probe")
        downloaded = await access.fetch(token, uploaded["url"])
        report["openwebui_upload_download"] = downloaded.data == image.data
        report["relative_attachment_url"] = uploaded["url"].startswith("/api/v1/files/")
        if chat:
            models = await client.list_models(token)
            available = []
            for model in models:
                meta = (model.get("info") or {}).get("meta") or {}
                if (meta.get("capabilities") or {}).get("vision") is not False:
                    available.append(model)
            chosen = next((m for m in available if m.get("id") == account["model_id"]), None)
            chosen = chosen or next(iter(available), None)
            if not chosen:
                report["chat_skipped"] = "no_vision_model"
                return report
            cfg.capabilities.follow_model_defaults = False
            for key in (
                "tools", "allow_terminal", "web_search", "image_generation", "code_interpreter", "memory",
            ):
                setattr(cfg.capabilities, key, False)
            cfg.reply.segment_interval = 0
            socket = OwuiSocket(cfg.owui.base_url, token)
            await socket.connect()
            await socket.join()
            output = []

            async def collect(uid, text):
                output.append(text)

            with tempfile.TemporaryDirectory(prefix="oui-image-probe-") as temporary:
                state = StateStore(Path(temporary) / "state.db")
                try:
                    state.set_focus("probe", model_id=chosen["id"])
                    runner = ChatRunner(cfg, state, client, socket, collect)
                    result = await asyncio.wait_for(runner.run_turn(
                        "probe", token, "联调测试：请只回答这张纯色图片的颜色，不调用工具。",
                        files=[uploaded],
                    ), timeout=100)
                    report["vision_turn_completed"] = result.ok
                    text = "\n".join(output).lower()
                    report["vision_color_correct"] = "红" in text or "red" in text
                    if result.chat_id:
                        detail = await client.get_chat(token, result.chat_id)
                        messages = detail.get("chat", detail).get("history", {}).get("messages", {}).values()
                        report["attachment_in_saved_user_message"] = any(
                            message.get("role") == "user"
                            and any(file.get("id") == uploaded["id"] for file in message.get("files", []))
                            for message in messages
                        )
                    if result.ok:
                        output.clear()
                        followup = await asyncio.wait_for(runner.run_turn(
                            "probe", token, "刚才那张图片是什么颜色？只回答颜色。",
                        ), timeout=100)
                        text = "\n".join(output).lower()
                        report["followup_completed"] = followup.ok
                        report["followup_color_correct"] = "红" in text or "red" in text
                finally:
                    state.close()
    except Exception as exc:
        report["errors"].append({"stage": "openwebui_image_probe", "type": type(exc).__name__})
    finally:
        if socket:
            await socket.close()
        clean = True
        if token:
            for chat_id in client.created_chats:
                try:
                    response = await client._client.delete(
                        f"{client.base_url}/api/v1/chats/{chat_id}", headers=client._auth(token),
                    )
                    clean = clean and response.status_code in (200, 204, 404)
                except Exception:
                    clean = False
            if uploaded:
                try:
                    response = await client._client.delete(
                        f"{client.base_url}/api/v1/files/{uploaded['id']}", headers=client._auth(token),
                    )
                    clean = clean and response.status_code in (200, 204, 404)
                except Exception:
                    clean = False
        report["test_resources_removed"] = clean
        await client.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--chat", action="store_true", help="Use an isolated administrator test chat, then delete it",
    )
    args = parser.parse_args()
    os.umask(0o077)
    report = asyncio.run(probe(args.data_dir, chat=args.chat))
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return int(bool(report["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
