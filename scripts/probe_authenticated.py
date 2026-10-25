"""Validate the real backend with a JWT supplied on stdin, never in argv or files."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
from open_webui_weixin.chat import ChatRunner
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, load_config
from open_webui_weixin.owui import OwuiClient
from open_webui_weixin.owui_socket import OwuiSocket
from open_webui_weixin.state import StateStore

ROOT = Path(__file__).resolve().parents[1]


def text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    return "".join(
        str(part.get("text") or "")
        for item in message.get("output") or []
        if item.get("type") == "message"
        for part in item.get("content") or []
        if part.get("type") in {"text", "output_text"}
    )


class RecordingClient(OwuiClient):
    def __init__(self, base_url: str):
        super().__init__(base_url, timeout=45)
        self.turns: list[dict] = []

    async def start_chat_completion(self, jwt_token: str, body: dict) -> dict:
        response = await super().start_chat_completion(jwt_token, body)
        self.turns.append({
            "chat_id": response.get("chat_id"),
            "assistant_id": body["id"],
            "parent_id": body.get("parent_id"),
            "memory_enabled": body.get("features", {}).get("memory", False),
        })
        return response


async def run(phase: str, credentials: dict, report_path: Path) -> int:
    token = credentials["jwt_token"]
    base_url = credentials["base_url"].rstrip("/")
    run_id = uuid.uuid4().hex[:10]
    report = {
        "date": datetime.datetime.now(datetime.UTC).isoformat(),
        "run_id": run_id,
        "phase": phase,
        "scope": "Local adapter connected to the real Open WebUI; no WeChat authorization yet",
        "target": base_url,
        "checks": [],
        "retained_chat_ids": [],
    }

    def save() -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def record(name: str, passed: bool, **evidence) -> None:
        report["checks"].append({"name": name, "passed": bool(passed), **evidence})
        save()
        print(json.dumps(report["checks"][-1], ensure_ascii=False), flush=True)

    owui = RecordingClient(base_url)
    http = httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=45
    )
    sockets = []
    stores = []
    seed_ids = set()
    try:
        session = await owui.whoami(token)
        if session.user_id != credentials["user_id"]:
            raise RuntimeError("Authenticated identity mismatch")
        config = await owui.get_app_config(token)
        record("authenticated_identity", True, role=session.role, version=config.get("version"))
        resolved = await owui.resolve_default_model(token)
        if not resolved:
            raise RuntimeError("No available model")
        model_id, model_name = resolved
        if credentials.get("model_id"):
            model_id = credentials["model_id"]
        report["model_id"] = model_id
        record("model_available", True, default_model_name=model_name, selected_model=model_id)
        before = await http.get("/api/v1/memories/")
        before.raise_for_status()
        existing_memory_ids = {item["id"] for item in before.json()}

        with tempfile.TemporaryDirectory(prefix="oui-wechat-live-") as folder:
            cfg = load_config(EXAMPLE_CONFIG_PATH)
            cfg.owui.base_url = base_url
            cfg.capabilities.allow_terminal = False
            cfg.capabilities.tools = False
            cfg.capabilities.web_search = False
            cfg.capabilities.image_generation = False
            cfg.capabilities.code_interpreter = False
            cfg.capabilities.memory = False
            cfg.reply.segment_interval = 0
            cfg.display.tool_status.enable = False

            async def make_runner(name: str):
                socket = OwuiSocket(base_url, token)
                sockets.append(socket)
                await socket.connect(timeout=15)
                ack = await socket.join()
                if ack.get("id") != session.user_id:
                    raise RuntimeError("Socket identity mismatch")
                store = StateStore(Path(folder) / name / "state.db")
                stores.append(store)
                store.set_focus(name, model_id=model_id)
                emitted = []

                async def capture(_uid, text):
                    emitted.append(text)

                return ChatRunner(cfg, store, owui, socket, capture), store, emitted

            runner, state, emitted = await make_runner("adapter")
            record("real_websocket_identity", True)

            async def turn(client, uid, prompt, label):
                try:
                    result = await asyncio.wait_for(
                        client.run_turn(uid, token, prompt), timeout=180
                    )
                except TimeoutError:
                    if owui.turns and owui.turns[-1].get("chat_id"):
                        await owui.stop_chat(token, owui.turns[-1]["chat_id"])
                    raise
                if not result.ok:
                    record(label, False, error=result.error)
                    raise RuntimeError("Chat turn failed; see the recorded check")
                last = owui.turns[-1]
                detail = await owui.get_chat(token, result.chat_id)
                messages = detail["chat"]["history"]["messages"]
                saved = messages.get(last["assistant_id"], {})
                record(label, bool(saved.get("done")) and bool(text_of(saved)),
                       chat_id=result.chat_id, assistant_saved=bool(saved))
                if not report["checks"][-1]["passed"]:
                    raise RuntimeError("Assistant response not persisted")
                if result.chat_id not in report["retained_chat_ids"]:
                    report["retained_chat_ids"].append(result.chat_id)
                    await owui.rename_chat(token, result.chat_id, f"OWUX 验收 {run_id} {label}")
                return result, saved

            if phase in {"chat", "all"}:
                marker = f"WEB_{run_id}"
                result, first = await turn(
                    runner, "adapter",
                    f"这是微信接入验收的临时会话。会话标记是 LOCAL_{run_id}。"
                    "只回复“收到”。不要保存为长期记忆。",
                    "persistent_chat",
                )
                first_sent = owui.turns[-1]
                detail = await owui.get_chat(token, result.chat_id)
                user_id = first.get("parentId")
                user_message = detail["chat"]["history"]["messages"].get(user_id, {})
                record("user_message_saved", f"LOCAL_{run_id}" in text_of(user_message))
                record("stream_matches_saved_reply", text_of(first) in "\n".join(emitted))

                web_runner, web_state, _ = await make_runner("web-client")
                web_state.set_focus(
                    "web-client", chat_id=result.chat_id,
                    leaf_id=first_sent["assistant_id"], is_first_message=False,
                )
                await turn(
                    web_runner, "web-client",
                    f"现在从另一个客户端补充：网页验证码是 {marker}。仅本会话使用，不存长期记忆。"
                    "只回复“收到”。",
                    "second_client_continuation",
                )
                web_reply_id = owui.turns[-1]["assistant_id"]
                old_leaf = state.get_focus("adapter")["leaf_id"]
                _, continued = await turn(
                    runner, "adapter", "刚才网页验证码是什么？仅回答验证码。",
                    "adapter_after_second_client",
                )
                record("fresh_server_parent",
                       old_leaf != web_reply_id and owui.turns[-1]["parent_id"] == web_reply_id)
                record("shared_conversation_context", marker in text_of(continued))

            if phase in {"memory", "all"}:
                cfg.capabilities.memory = True
                read_marker = f"OWUXMEM_{run_id}"
                write_marker = f"WXWRITE_{run_id}"
                response = await http.post("/api/v1/memories/add", json={
                    "content": f"本账号微信接入验收的虚构代号是 {read_marker}，仅测试数据。",
                    "type": "user", "path": f"validation/{run_id}/read",
                })
                response.raise_for_status()
                seed_ids.add(response.json()["id"])
                record("web_memory_created", True)
                state.clear_focus_chat("adapter")
                _, recalled = await turn(
                    runner, "adapter",
                    "我之前保存的微信接入验收虚构代号是什么？只回答代号。",
                    "new_chat_memory_read",
                )
                record("web_memory_visible_to_adapter",
                       owui.turns[-1]["memory_enabled"] and read_marker in text_of(recalled))
                await turn(
                    runner, "adapter",
                    "请实际调用内置记忆更新工具，将以下测试信息保存为用户长期记忆："
                    f"微信接入验收的另一个虚构代号是 {write_marker}。"
                    f"可放入 validation/{run_id}/write 路径。这是测试数据，不代表个人真实情况。"
                    "保存后简短确认；请不要修改、删除其他已有记忆。",
                    "adapter_memory_write",
                )
                written = []
                for _ in range(15):
                    response = await http.get("/api/v1/memories/")
                    response.raise_for_status()
                    written = [
                        item for item in response.json()
                        if item["id"] not in existing_memory_ids
                        and write_marker in item.get("content", "")
                    ]
                    if written:
                        break
                    await asyncio.sleep(1)
                seed_ids.update(item["id"] for item in written)
                record("adapter_memory_persisted_in_web_backend", bool(written),
                       matched_records=len(written))
    except (RuntimeError, KeyError, ValueError, httpx.HTTPError, TimeoutError) as exc:
        record("probe_completed", False, error_type=type(exc).__name__)
    finally:
        # Delete only synthetic records bearing this run's marker; preserve all prior data.
        try:
            response = await http.get("/api/v1/memories/")
            if response.is_success and isinstance(response.json(), list):
                for item in response.json():
                    if run_id in item.get("content", ""):
                        seed_ids.add(item["id"])
            removed = 0
            for memory_id in seed_ids:
                response = await http.delete(f"/api/v1/memories/{memory_id}")
                response.raise_for_status()
                removed += int(response.json() is True)
            if seed_ids:
                record("synthetic_memory_cleanup", removed == len(seed_ids), removed_records=removed)
        except (httpx.HTTPError, ValueError) as exc:
            record("synthetic_memory_cleanup", False, error_type=type(exc).__name__)
        for socket in sockets:
            await socket.close()
        for store in stores:
            store.close()
        await http.aclose()
        await owui.close()
        save()
    return int(any(not item["passed"] for item in report["checks"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("chat", "memory", "all"), default="all")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "live-openwebui.json")
    args = parser.parse_args()
    credentials = json.load(sys.stdin)
    return asyncio.run(run(args.phase, credentials, args.report))


if __name__ == "__main__":
    raise SystemExit(main())
