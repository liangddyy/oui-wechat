from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from open_webui_weixin.owui import OwuiError, SessionInfo

from oui_wechat.auth import AdminAuth
from oui_wechat.service import ServiceManager
from oui_wechat.web import AUTH, MANAGER, create_app

ADMIN_PASSWORD = "test-admin-password"


class FakeOwui:
    def __init__(self):
        self.sessions = {}

    async def signin(self, email, password):
        if password != "fixture-password":
            raise OwuiError("invalid credentials", status_code=400)
        name = email.split("@")[0]
        session = SessionInfo(f"jwt-secret-{name}", None, f"user-{name}", email, name, "user")
        self.sessions[session.jwt_token] = session
        return session

    async def resolve_default_model(self, token):
        name = self.sessions[token].name
        return f"model-{name}", f"模型 {name}"

    async def list_models(self, token):
        name = self.sessions[token].name
        return [
            {"id": f"model-{name}", "name": f"模型 {name}", "info": None},
            {"id": "hidden-model", "info": {"meta": {"hidden": True}}},
        ]

    async def whoami(self, token):
        if token not in self.sessions:
            raise OwuiError("expired", status_code=401)
        return self.sessions[token]

    async def close(self):
        pass


class TestManager(ServiceManager):
    def __init__(self, config):
        super().__init__(config)
        self.original_client = self.owui
        self.owui = FakeOwui()

    async def reconcile(self):
        pass

    async def _start(self):
        pass

    async def health(self, force=False):
        return {"connected": True, "version": "0.12.0"}

    async def close(self):
        await super().close()
        await self.original_client.close()


class FakeIlink:
    def __init__(self, require_code=False):
        self.require_code = require_code
        self.closed = False
        self.received_code = None

    async def get_bot_qrcode(self, bot_type, tokens):
        return {"qrcode": "private-qr-secret", "qrcode_img_content": "https://example.com/pair"}

    async def poll_qrcode_status(self, qr, code=None):
        if self.require_code and code is None:
            return {"status": "need_verifycode"}
        self.received_code = code
        return {
            "status": "confirmed", "bot_token": "private-bot-token",
            "ilink_bot_id": "paired-bot", "ilink_user_id": "paired-user",
            "baseurl": "https://ilinkai.weixin.qq.com",
        }

    async def close(self):
        self.closed = True


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oui-web-test-")
        self.directory = Path(self.temporary.name)
        self.app = create_app(self.directory, manager_factory=TestManager)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"X-OUI-Request": "1"}
        self.manager = self.app[MANAGER]

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    async def setup_admin(self):
        response = await self.client.post(
            "/api/auth/setup", json={"password": ADMIN_PASSWORD}, headers=self.headers
        )
        self.assertEqual(response.status, 200, await response.text())
        return response

    def add_account(self, name):
        self.manager.state.save_login({
            "bot_id": f"bot-{name}", "bot_token": f"bot-secret-{name}",
            "scanner_user_id": f"wx-{name}", "base_url": "https://example.com",
        })

    async def bind(self, name):
        self.add_account(name)
        response = await self.client.post(
            f"/api/accounts/bot-{name}/bind",
            json={"email": f"{name}@example.com", "password": "fixture-password"},
            headers=self.headers,
        )
        self.assertEqual(response.status, 200, await response.text())

    async def test_unauthenticated_routes_do_not_expose_configuration(self):
        for path in ("/api/accounts", "/api/settings", "/api/logs", "/api/status"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 401)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await self.client.get("/health")).status, 200)

    async def test_setup_password_hash_permissions_and_cookie(self):
        response = await self.setup_admin()
        cookie = response.cookies["oui_session"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertNotIn(ADMIN_PASSWORD, (self.directory / "admin.json").read_text())
        self.assertEqual((self.directory / "admin.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        restored = AdminAuth(self.directory / "admin.json")
        self.assertFalse(restored.valid(cookie.value))
        self.assertTrue(await restored.login(ADMIN_PASSWORD, "test"))

    async def test_setup_cannot_be_replaced_by_another_request(self):
        await self.setup_admin()
        response = await self.client.post(
            "/api/auth/setup", json={"password": "replacement-password"}, headers=self.headers
        )
        self.assertEqual(response.status, 400)
        self.assertTrue(await self.app[AUTH].login(ADMIN_PASSWORD, "test"))

    async def test_missing_csrf_header_and_wrong_origin_are_rejected(self):
        await self.setup_admin()
        response = await self.client.post("/api/service/stop", json={})
        self.assertEqual(response.status, 403)
        response = await self.client.post(
            "/api/service/stop", json={},
            headers={**self.headers, "Origin": "https://untrusted.example"},
        )
        self.assertEqual(response.status, 403)
        self.assertTrue(self.manager.enabled)

    async def test_password_change_revokes_previous_sessions(self):
        response = await self.setup_admin()
        old_token = response.cookies["oui_session"].value
        response = await self.client.post(
            "/api/auth/password", headers=self.headers,
            json={"current": ADMIN_PASSWORD, "password": "new-test-password"},
        )
        self.assertEqual(response.status, 200)
        self.assertFalse(self.app[AUTH].valid(old_token))
        self.assertTrue(self.app[AUTH].valid(response.cookies["oui_session"].value))
        with self.assertRaises(ValueError):
            await self.app[AUTH].login(ADMIN_PASSWORD, "test")

    async def test_login_attempts_are_limited(self):
        await self.setup_admin()
        await self.client.post("/api/auth/logout", json={}, headers=self.headers)
        for _ in range(6):
            response = await self.client.post(
                "/api/auth/login", json={"password": "wrong-password"}, headers=self.headers
            )
        self.assertEqual(response.status, 400)
        self.assertIn("频繁", (await response.json())["error"])

    async def test_reasoning_defaults_on_without_detailed_content(self):
        await self.setup_admin()
        settings = await (await self.client.get("/api/settings")).json()
        self.assertEqual(settings["display"]["reasoning"], {"enable": True, "detailed": False})
        self.assertFalse(settings["capabilities"]["allow_terminal"])

    async def test_display_settings_persist_without_replacing_other_configuration(self):
        await self.setup_admin()
        original_weixin = self.manager.config.read()["weixin"]
        response = await self.client.put(
            "/api/settings", headers=self.headers,
            json={"display": {"reasoning": {"enable": False}}, "reply": {"max_length": 512}},
        )
        self.assertEqual(response.status, 200, await response.text())
        saved = self.manager.config.read()
        self.assertFalse(saved["display"]["reasoning"]["enable"])
        self.assertEqual(saved["weixin"], original_weixin)
        self.assertEqual(saved["reply"]["max_length"], 512)

    async def test_invalid_settings_do_not_overwrite_configuration(self):
        await self.setup_admin()
        previous = self.manager.config.path.read_bytes()
        for data in (
            {"reply": {"max_length": 1}},
            {"reply": {"segment_interval": -1}},
            {"display": {"typing": "yes"}},
            {"owui": {"base_url": "https://user:password@example.com"}},
        ):
            response = await self.client.put("/api/settings", json=data, headers=self.headers)
            self.assertEqual(response.status, 400)
            self.assertEqual(previous, self.manager.config.path.read_bytes())

    async def test_two_users_bind_independently_without_storing_passwords(self):
        await self.setup_admin()
        await self.bind("alice")
        await self.bind("bob")
        a = self.manager.state.get_binding("wx-alice")
        b = self.manager.state.get_binding("wx-bob")
        self.assertEqual(a["owui_user_id"], "user-alice")
        self.assertEqual(b["owui_user_id"], "user-bob")
        self.assertEqual(a["owui_password"], "")
        self.assertEqual(b["owui_password"], "")
        body = await (await self.client.get("/api/accounts")).text()
        self.assertNotIn("jwt-secret", body)
        self.assertNotIn("bot-secret", body)
        self.assertNotIn("scanner_user_id", body)

    async def test_wrong_credentials_leave_previous_binding_untouched(self):
        await self.setup_admin()
        await self.bind("alice")
        before = dict(self.manager.state.get_binding("wx-alice"))
        response = await self.client.post(
            "/api/accounts/bot-alice/bind", headers=self.headers,
            json={"email": "bob@example.com", "password": "incorrect"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(dict(self.manager.state.get_binding("wx-alice")), before)

    async def test_bound_tokens_are_not_sent_to_a_changed_server(self):
        await self.setup_admin()
        await self.bind("alice")
        old_url = self.manager.config.cfg.owui.base_url
        response = await self.client.put(
            "/api/settings", json={"owui": {"base_url": "https://another.example"}},
            headers=self.headers,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.manager.config.cfg.owui.base_url, old_url)

    async def test_model_selection_respects_per_user_permissions(self):
        await self.setup_admin()
        await self.bind("alice")
        data = await (await self.client.get("/api/accounts/bot-alice/models")).json()
        self.assertEqual([item["id"] for item in data["models"]], ["model-alice"])
        response = await self.client.post(
            "/api/accounts/bot-alice/model", json={"model_id": "model-bob"}, headers=self.headers
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.manager.state.get_focus("wx-alice")["model_id"], "model-alice")

    async def test_remove_account_preserves_other_users(self):
        await self.setup_admin()
        await self.bind("alice")
        await self.bind("bob")
        response = await self.client.post(
            "/api/accounts/bot-alice/remove", json={}, headers=self.headers
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.manager.state.get_binding("wx-alice"))
        self.assertEqual(self.manager.state.get_binding("wx-bob")["jwt_token"], "jwt-secret-bob")
        self.assertEqual(len(self.manager.state.load_accounts()), 1)

    async def test_unbind_and_rebind_clear_previous_user_focus_and_snapshots(self):
        await self.setup_admin()
        await self.bind("alice")
        self.manager.state.set_focus("wx-alice", chat_id="private-old-chat", leaf_id="old")
        self.manager.state.save_snapshot("wx-alice", "chats", [("private-old-chat", "Private")])
        response = await self.client.post(
            "/api/accounts/bot-alice/bind", headers=self.headers,
            json={"email": "bob@example.com", "password": "fixture-password"},
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.manager.state.get_focus("wx-alice")["chat_id"])
        self.assertIsNone(self.manager.state.resolve_snapshot("wx-alice", "chats", 1))

    async def test_logs_redact_credentials(self):
        await self.setup_admin()
        logging.getLogger("oui.fixture").warning(
            "Bearer pretend-auth-token password=private-pass jwt_token='private-jwt' /login a@b.c pass"
        )
        response = await self.client.get("/api/logs")
        body = await response.text()
        for secret in ("pretend-auth-token", "private-pass", "private-jwt", "a@b.c"):
            self.assertNotIn(secret, body)
        self.assertIn("redacted", body)

    async def test_failed_adapter_can_be_stopped_without_leaking_clients(self):
        await self.setup_admin()
        fake = FakeIlink()

        async def failed_task():
            await asyncio.sleep(0.01)
            raise RuntimeError("fixture failure")

        self.manager.adapter = SimpleNamespace(request_stop=lambda: None)
        self.manager.task = asyncio.create_task(failed_task())
        self.manager.login_client = fake
        response = await self.client.post("/api/service/stop", json={}, headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        self.assertIsNone(self.manager.task)
        self.assertTrue(fake.closed)
        self.assertFalse(self.manager.enabled)

    async def test_cancelled_pairing_closes_client_without_creating_an_account(self):
        await self.setup_admin()
        fake = FakeIlink()
        fake.poll_qrcode_status = AsyncMock(return_value={"status": "wait"})
        with patch("oui_wechat.service.build_client", return_value=fake):
            response = await self.client.post("/api/pairings", json={}, headers=self.headers)
            job_id = (await response.json())["id"]
            await asyncio.sleep(0.01)
            response = await self.client.delete(f"/api/pairings/{job_id}", headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertTrue(fake.closed)
            self.assertEqual(self.manager.jobs[job_id].status, "cancelled")
            self.assertEqual(self.manager.state.load_accounts(), [])

    async def test_browser_pairing_accepts_verification_code_and_does_not_leak_tokens(self):
        await self.setup_admin()
        fake = FakeIlink(require_code=True)
        with patch("oui_wechat.service.build_client", return_value=fake):
            response = await self.client.post("/api/pairings", json={}, headers=self.headers)
            self.assertEqual(response.status, 201)
            job_id = (await response.json())["id"]
            await asyncio.sleep(0.01)
            response = await self.client.get(f"/api/pairings/{job_id}")
            self.assertEqual((await response.json())["status"], "need_verifycode")
            image = await self.client.get(f"/api/pairings/{job_id}/qr")
            self.assertTrue((await image.read()).startswith(b"\x89PNG"))
            response = await self.client.post(
                f"/api/pairings/{job_id}/code", json={"code": "bad"}, headers=self.headers
            )
            self.assertEqual(response.status, 400)
            response = await self.client.post(
                f"/api/pairings/{job_id}/code", json={"code": "123456"}, headers=self.headers
            )
            self.assertEqual(response.status, 200)
            await asyncio.wait_for(self.manager.jobs[job_id].task, timeout=2)
            response = await self.client.get(f"/api/pairings/{job_id}")
            body = await response.text()
            self.assertEqual(json.loads(body)["status"], "confirmed")
            self.assertNotIn("private-bot-token", body)
            self.assertNotIn("private-qr-secret", body)
            self.assertEqual(fake.received_code, "123456")
            self.assertTrue(fake.closed)

    async def test_static_app_has_local_assets_and_content_security_policy(self):
        response = await self.client.get("/")
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("/static/app.js", body)
        self.assertNotIn("onclick=", body)
        self.assertNotIn("cdn.", body)
        self.assertEqual((await self.client.get("/static/app.js")).status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
