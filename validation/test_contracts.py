"""Exercise real Socket.IO transport and cross-client conversation contracts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import socketio
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "open-webui-weixin" / "tests"))

from open_webui_weixin.owui import OwuiError  # noqa: E402
from open_webui_weixin.owui_socket import OwuiSocket  # noqa: E402
from test_chat import ev, make, run_turn_with_events  # noqa: E402


class SocketContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = socketio.AsyncServer(
            async_mode="aiohttp", always_connect=True
        )
        self.server_sid = None

        @self.server.event
        async def connect(sid, environ, auth):
            self.server_sid = sid

        @self.server.on("user-join")
        async def join(sid, data):
            return {"id": "local-test-user", "name": "Local Test"}

        app = web.Application()
        self.server.attach(app, socketio_path="/ws/socket.io")
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "127.0.0.1", 0)
        await site.start()
        port = self.web_runner.addresses[0][1]
        self.client = OwuiSocket(f"http://127.0.0.1:{port}", "local-fake-jwt")
        await self.client.connect(timeout=3)
        await self.client.join()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.shutdown()
        await self.web_runner.cleanup()

    async def test_session_id_matches_server_socket_id(self) -> None:
        self.assertEqual(
            self.client.sid,
            self.server_sid,
            "Completion session_id must be the Socket.IO ID, not the Engine.IO ID",
        )

    async def test_server_can_call_the_advertised_session(self) -> None:
        response = await self.server.call(
            "events",
            {"data": {"type": "request:terminal:state", "data": {}}},
            to=self.client.sid,
            timeout=0.5,
        )
        self.assertEqual(response, {"connected": False})


class ConversationContracts(unittest.IsolatedAsyncioTestCase):
    async def test_unreadable_history_does_not_generate_or_change_focus(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oui-wechat-contract-") as folder:
            runner, _sock, rest, state, _sent = await make(Path(folder))
            try:
                state.set_focus("wx1", chat_id="shared-chat", leaf_id="old-leaf")
                rest.get_chat = AsyncMock(side_effect=OwuiError("unavailable", status_code=503))
                result = await runner.run_turn("wx1", "jwt", "hello")
                self.assertFalse(result.ok)
                self.assertFalse(rest.bodies)
                self.assertEqual(state.get_focus("wx1")["leaf_id"], "old-leaf")
            finally:
                state.close()

    async def test_busy_web_reply_is_not_used_as_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oui-wechat-contract-") as folder:
            runner, _sock, rest, state, _sent = await make(Path(folder))
            try:
                state.set_focus("wx1", chat_id="shared-chat", leaf_id="old-leaf")
                rest.get_chat = AsyncMock(return_value={"chat": {"history": {
                    "currentId": "still-generating",
                    "messages": {"still-generating": {"role": "assistant", "done": False}},
                }}})
                result = await runner.run_turn("wx1", "jwt", "hello")
                self.assertFalse(result.ok)
                self.assertIn("仍在生成", result.error)
                self.assertFalse(rest.bodies)
                self.assertEqual(state.get_focus("wx1")["leaf_id"], "old-leaf")
            finally:
                state.close()

    async def test_web_continuation_is_used_before_next_wechat_message(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oui-wechat-contract-") as folder:
            runner, sock, rest, state, _sent = await make(Path(folder))
            try:
                state.set_focus(
                    "wx1",
                    chat_id="shared-chat",
                    leaf_id="old-wechat-reply",
                    is_first_message=False,
                )
                rest.get_chat = AsyncMock(return_value={
                    "id": "shared-chat",
                    "chat": {
                        "history": {
                            "currentId": "new-web-reply",
                            "messages": {
                                "old-wechat-reply": {
                                    "id": "old-wechat-reply",
                                    "role": "assistant",
                                    "done": True,
                                },
                                "new-web-question": {
                                    "id": "new-web-question",
                                    "parentId": "old-wechat-reply",
                                    "role": "user",
                                },
                                "new-web-reply": {
                                    "id": "new-web-reply",
                                    "parentId": "new-web-question",
                                    "role": "assistant",
                                    "done": True,
                                },
                            },
                        },
                    },
                })
                body, result = await run_turn_with_events(
                    runner,
                    sock,
                    rest,
                    "wx1",
                    "Continue after my web conversation",
                    [ev("chat:completion", {"done": True})],
                )
                self.assertIsNotNone(result)
                self.assertTrue(result.ok)
                self.assertEqual(
                    body["parent_id"],
                    "new-web-reply",
                    "Using the cached old leaf silently forks before the web continuation",
                )
                self.assertEqual(body["user_message"]["parentId"], "new-web-reply")
                rest.get_chat.assert_awaited_once()
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
