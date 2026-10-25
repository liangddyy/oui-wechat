from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import TestClient, TestServer
from open_webui_weixin.owui import OwuiError, SessionInfo

from oui_wechat.adapter import WebAdapter, WebCommands
from oui_wechat.auth import AdminAuth, LoginLimiter, RateLimited
from oui_wechat.proxy import ProxyPolicy
from oui_wechat.service import ServiceManager
from oui_wechat.web import AUTH, MANAGER, PROXY, create_app

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
        if not isinstance(self.owui, FakeOwui):
            await self.owui.close()
            self.owui = FakeOwui()

    async def health(self, force=False):
        return {"connected": True, "version": "0.12.0"}

    async def close(self):
        await super().close()
        await self.original_client.close()


class FakeIlink:
    def __init__(self, require_code=False, name="paired"):
        self.require_code = require_code
        self.closed = False
        self.received_code = None
        self.name = name

    async def get_bot_qrcode(self, bot_type, tokens):
        return {"qrcode": "private-qr-secret", "qrcode_img_content": "https://example.com/pair"}

    async def poll_qrcode_status(self, qr, code=None):
        if self.require_code and code is None:
            return {"status": "need_verifycode"}
        self.received_code = code
        return {
            "status": "confirmed", "bot_token": "private-bot-token",
            "ilink_bot_id": f"{self.name}-bot", "ilink_user_id": f"{self.name}-user",
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
        self.other_clients = []

    async def asyncTearDown(self):
        for client in self.other_clients:
            await client.close()
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
        restored.limiter.close()

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
        self.assertEqual(response.status, 429)
        self.assertIn("频繁", (await response.json())["error"])
        self.assertGreater(int(response.headers["Retry-After"]), 0)

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

    def enable_users(self):
        self.manager.config.write(self.manager.config.merge({"access": {"user_login": True}}))

    async def user_client(self, name="alice"):
        client = ClientSession(
            base_url=self.client.make_url("/"), cookie_jar=CookieJar(unsafe=True)
        )
        self.other_clients.append(client)
        response = await client.post(
            "/api/auth/owui", headers=self.headers,
            json={"email": f"{name}@example.com", "password": "fixture-password"},
        )
        self.assertEqual(response.status, 200, await response.text())
        return client

    async def pair_user(self, client, name="alice", require_code=False):
        fake = FakeIlink(require_code=require_code, name=name)
        with patch("oui_wechat.service.build_client", return_value=fake):
            response = await client.post("/api/pairings", json={}, headers=self.headers)
            self.assertEqual(response.status, 201, await response.text())
            job = self.manager.jobs[(await response.json())["id"]]
            if require_code:
                await asyncio.sleep(0.01)
            else:
                await asyncio.wait_for(job.task, timeout=2)
        return job, fake

    async def test_user_login_requires_admin_to_enable_it(self):
        await self.setup_admin()
        response = await self.client.post(
            "/api/auth/owui", headers=self.headers,
            json={"email": "alice@example.com", "password": "fixture-password"},
        )
        self.assertEqual(response.status, 403)
        response = await self.client.put(
            "/api/settings", headers=self.headers, json={"access": {"user_login": True}}
        )
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["access"]["user_login"])
        state = await (await self.client.get("/api/auth/state")).json()
        self.assertTrue(state["user_login"])
        self.assertEqual(state["role"], "admin")

    async def test_users_only_see_own_accounts_and_admin_sees_all(self):
        await self.setup_admin()
        await self.bind("alice")
        await self.bind("bob")
        self.enable_users()
        alice = await self.user_client()
        bob = await self.user_client("bob")
        for client, expected in ((alice, "alice"), (bob, "bob")):
            response = await client.get("/api/accounts")
            data = await response.json()
            self.assertEqual([row["id"] for row in data["accounts"]], [f"bot-{expected}"])
            self.assertNotIn("jwt-secret", json.dumps(data))
            self.assertNotIn("bot-secret", json.dumps(data))
            status = await (await client.get("/api/status")).json()
            self.assertEqual(status["accounts"], 1)
            self.assertEqual(status["bound_accounts"], 1)
        admin = await (await self.client.get("/api/accounts")).json()
        self.assertEqual(len(admin["accounts"]), 2)

    async def test_legacy_bindings_and_owner_reservations_survive_manager_restart(self):
        await self.setup_admin()
        await self.bind("alice")
        self.enable_users()
        alice = await self.user_client()
        await alice.post("/api/accounts/bot-alice/unbind", headers=self.headers, json={})
        restored = TestManager(self.manager.config)
        principal = next(p for p in self.app[AUTH].sessions.values() if p.role == "user")
        try:
            self.assertTrue(restored.allowed_account("bot-alice", principal))
            self.assertEqual(len(restored.accounts(principal)), 1)
        finally:
            await restored.close()

    async def test_admin_can_reassign_a_connection_and_previous_owner_loses_access(self):
        await self.setup_admin()
        await self.bind("alice")
        self.enable_users()
        alice = await self.user_client()
        bob = await self.user_client("bob")
        response = await self.client.post(
            "/api/accounts/bot-alice/bind", headers=self.headers,
            json={"email": "bob@example.com", "password": "fixture-password"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await (await alice.get("/api/accounts")).json())["accounts"], [])
        self.assertEqual((await alice.get("/api/accounts/bot-alice/models")).status, 404)
        self.assertEqual(len((await (await bob.get("/api/accounts")).json())["accounts"]), 1)

    async def test_inbound_message_waits_for_rebinding_to_finish_before_using_credentials(self):
        await self.setup_admin()
        await self.bind("alice")
        adapter = WebAdapter(
            self.manager.config.cfg, self.manager.state, FakeIlink(), self.manager.owui,
            access_lock=self.manager.access_lock,
        )
        switching = asyncio.Event()
        finish = asyncio.Event()
        seen_identities = []

        async def delayed_drop(uid):
            switching.set()
            await finish.wait()

        async def dispatched(handle, message):
            seen_identities.append(self.manager.state.get_binding("wx-alice")["owui_user_id"])

        with patch.object(self.manager, "_drop_runtime", side_effect=delayed_drop), patch(
            "open_webui_weixin.adapter.Adapter._dispatch", side_effect=dispatched
        ):
            rebind = asyncio.create_task(self.client.post(
                "/api/accounts/bot-alice/bind", headers=self.headers,
                json={"email": "bob@example.com", "password": "fixture-password"},
            ))
            await asyncio.wait_for(switching.wait(), timeout=2)
            message = asyncio.create_task(adapter._dispatch(None, {}))
            await asyncio.sleep(0.01)
            self.assertEqual(seen_identities, [])
            finish.set()
            self.assertEqual((await rebind).status, 200)
            await message
        self.assertEqual(seen_identities, ["user-bob"])

    async def test_expired_session_cannot_complete_its_pending_scan(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        job, _ = await self.pair_user(alice, require_code=True)
        job.principal.expires_at = 0
        job.code.put_nowait("123456")
        await asyncio.wait_for(job.task, timeout=2)
        self.assertEqual(job.status, "error")
        self.assertEqual(self.manager.state.load_accounts(), [])

    async def test_user_cannot_access_admin_endpoints_or_foreign_account_actions(self):
        await self.setup_admin()
        await self.bind("bob")
        self.enable_users()
        alice = await self.user_client()
        for path in ("/api/settings", "/api/logs"):
            self.assertEqual((await alice.get(path)).status, 403)
        for method, path in (
            ("PUT", "/api/settings"), ("POST", "/api/service/stop"),
            ("POST", "/api/service/start"), ("POST", "/api/auth/password"),
        ):
            response = await alice.request(method, path, json={}, headers=self.headers)
            self.assertEqual(response.status, 403, path)
        for action in ("bind", "unbind", "remove", "rename", "model", "new-chat", "verify"):
            response = await alice.post(
                f"/api/accounts/bot-bob/{action}", json={}, headers=self.headers
            )
            self.assertEqual(response.status, 404, action)
        self.assertEqual((await alice.get("/api/accounts/bot-bob/models")).status, 404)
        self.assertTrue(self.manager.enabled)
        self.assertEqual(self.manager.state.get_binding("wx-bob")["owui_user_id"], "user-bob")

    async def test_user_scan_binds_own_identity_without_accepting_other_credentials(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        job, _ = await self.pair_user(alice)
        self.assertEqual(job.status, "confirmed", job.error)
        binding = self.manager.state.get_binding("alice-user")
        self.assertEqual(binding["owui_user_id"], "user-alice")
        self.assertEqual(binding["owui_password"], "")
        response = await alice.post(
            "/api/accounts/alice-bot/bind", headers=self.headers,
            json={"email": "bob@example.com", "password": "fixture-password"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.manager.state.get_binding("alice-user")["owui_user_id"], "user-alice")
        response = await alice.post(
            "/api/accounts/alice-bot/unbind", headers=self.headers, json={}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len((await (await alice.get("/api/accounts")).json())["accounts"]), 1)
        response = await alice.post("/api/accounts/alice-bot/bind", headers=self.headers, json={})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.manager.state.get_binding("alice-user")["owui_user_id"], "user-alice")

    async def test_foreign_rescan_cannot_replace_authorization_even_after_unbind_or_remove(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        bob = await self.user_client("bob")
        await self.pair_user(alice)
        original = dict(self.manager.account("alice-bot"))
        response = await alice.post("/api/accounts/alice-bot/unbind", headers=self.headers, json={})
        self.assertEqual(response.status, 200)
        job, _ = await self.pair_user(bob, "alice")
        self.assertEqual(job.status, "error")
        self.assertIn("其他用户", job.error)
        self.assertEqual(dict(self.manager.account("alice-bot")), original)
        await alice.post("/api/accounts/alice-bot/remove", headers=self.headers, json={})
        job, _ = await self.pair_user(bob, "alice")
        self.assertEqual(job.status, "error")
        self.assertEqual(self.manager.state.load_accounts(), [])
        job, _ = await self.pair_user(alice, "alice")
        self.assertEqual(job.status, "confirmed")

    async def test_pairing_status_qr_code_and_cancel_are_owner_scoped(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        bob = await self.user_client("bob")
        job, _ = await self.pair_user(alice, require_code=True)
        path = f"/api/pairings/{job.id}"
        for method, target in (("GET", path), ("GET", f"{path}/qr"),
                               ("DELETE", path), ("POST", f"{path}/code")):
            response = await bob.request(method, target, json={"code": "123456"}, headers=self.headers)
            self.assertEqual(response.status, 404)
        self.assertEqual((await self.client.get(path)).status, 200)
        self.assertEqual((await alice.get(path)).status, 200)
        response = await alice.post(f"{path}/code", json={"code": "123456"}, headers=self.headers)
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(job.task, timeout=2)
        self.assertEqual(job.status, "confirmed", job.error)
        self.assertEqual((await alice.get(path)).status, 200)

    async def test_user_logout_cancels_pending_pairing(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        job, fake = await self.pair_user(alice, require_code=True)
        response = await alice.post("/api/auth/logout", json={}, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(job.status, "cancelled")
        self.assertTrue(fake.closed)
        self.assertEqual(self.manager.state.load_accounts(), [])
        self.assertEqual((await alice.get("/api/accounts")).status, 401)

    async def test_disabling_user_login_revokes_existing_user_sessions(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        response = await self.client.put(
            "/api/settings", headers=self.headers, json={"access": {"user_login": False}}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await alice.get("/api/accounts")).status, 401)
        self.assertEqual((await self.client.get("/api/accounts")).status, 200)

    async def test_reenabling_user_login_does_not_revive_sessions_or_pending_scans(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        job, _ = await self.pair_user(alice, require_code=True)
        response = await self.client.put(
            "/api/settings", headers=self.headers, json={"access": {"user_login": False}}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(job.status, "cancelled")
        response = await self.client.put(
            "/api/settings", headers=self.headers, json={"access": {"user_login": True}}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await alice.get("/api/accounts")).status, 401)

    async def test_backend_change_invalidates_user_session_before_sending_token(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        self.manager.config.write(self.manager.config.merge({
            "owui": {"base_url": "https://new-server.example"},
        }))
        with patch.object(self.manager.owui, "whoami", new_callable=AsyncMock) as whoami:
            self.assertEqual((await alice.get("/api/accounts")).status, 401)
            whoami.assert_not_awaited()

    async def test_pending_and_expired_openwebui_accounts_cannot_log_in(self):
        await self.setup_admin()
        self.enable_users()
        for session in (
            SessionInfo("token", None, "u", "a@b.c", "Pending", "pending"),
            SessionInfo("token", 1, "u", "a@b.c", "Expired", "user"),
            SessionInfo("token", None, "", "a@b.c", "No identity", "user"),
        ):
            with patch.object(self.manager.owui, "signin", AsyncMock(return_value=session)):
                response = await self.client.post(
                    "/api/auth/owui", headers=self.headers,
                    json={"email": "a@b.c", "password": "fixture-password"},
                )
                self.assertEqual(response.status, 400)

    async def test_openwebui_admin_login_never_grants_local_admin_privileges(self):
        await self.setup_admin()
        self.enable_users()
        session = SessionInfo("token", None, "u", "admin@example.com", "Admin", "admin")
        with patch.object(self.manager.owui, "signin", AsyncMock(return_value=session)):
            response = await self.client.post(
                "/api/auth/owui", headers=self.headers,
                json={"email": "admin@example.com", "password": "fixture-password"},
            )
        self.assertEqual((await response.json())["role"], "user")
        self.assertEqual((await self.client.get("/api/logs")).status, 403)

    async def test_revoked_user_token_is_rechecked_and_does_not_remove_wechat(self):
        await self.setup_admin()
        self.enable_users()
        alice = await self.user_client()
        await self.pair_user(alice)
        principal = next(p for p in self.app[AUTH].sessions.values() if p.role == "user")
        principal.checked_at = 0
        with patch.object(self.manager.owui, "whoami", AsyncMock(
            side_effect=OwuiError("revoked", status_code=401)
        )):
            self.assertEqual((await alice.get("/api/accounts")).status, 401)
        self.assertEqual(len(self.manager.state.load_accounts()), 1)

    async def test_successful_logins_do_not_reset_attempt_budget_and_email_is_normalized(self):
        await self.setup_admin()
        self.enable_users()
        for index in range(6):
            response = await self.client.post(
                "/api/auth/owui", headers=self.headers,
                json={"email": " Alice@Example.COM " if index % 2 else "alice@example.com",
                      "password": "fixture-password"},
            )
            self.assertEqual(response.status, 200 if index < 5 else 429)
        body = await response.json()
        self.assertGreater(body["retry_after"], 0)
        self.assertEqual(body["retry_after"], int(response.headers["Retry-After"]))

    async def test_parallel_user_signin_requests_have_bounded_work(self):
        await self.setup_admin()
        self.enable_users()
        async def slow_signin(email, password):
            await asyncio.sleep(0.05)
            raise OwuiError("bad credentials", status_code=400)

        with patch.object(self.manager.owui, "signin", AsyncMock(side_effect=slow_signin)) as signin:
            responses = await asyncio.gather(*(
                self.client.post("/api/auth/owui", headers=self.headers,
                                 json={"email": f"user{i}@example.com", "password": "bad"})
                for i in range(10)
            ))
        self.assertEqual(signin.await_count, 4)
        self.assertEqual(sum(r.status == 429 for r in responses), 6)

    async def test_wechat_credential_commands_cannot_bypass_web_binding(self):
        command = WebCommands(
            self.manager.config.cfg, self.manager.state, self.manager.owui, SimpleNamespace()
        )
        with patch.object(self.manager.owui, "signin", new_callable=AsyncMock) as signin:
            for text in ("/login bob@example.com password", "/LOGIN x y", "/login-refresh",
                         "/logout", "/relogin", "/ldap x y"):
                self.assertIn("网页", await command.handle("uid", text))
            signin.assert_not_awaited()
        self.assertIsNone(self.manager.state.get_binding("uid"))

    async def test_forwarded_headers_require_explicit_trust(self):
        await self.setup_admin()
        response = await self.client.post(
            "/api/auth/login", json={"password": ADMIN_PASSWORD},
            headers={**self.headers, "X-Forwarded-Proto": "https", "X-Forwarded-For": "1.2.3.4"},
        )
        self.assertFalse(response.cookies["oui_session"]["secure"])
        with patch.dict(os.environ, {"OUI_TRUSTED_PROXIES": "127.0.0.1/32"}):
            self.app[PROXY].networks = ProxyPolicy().networks
        response = await self.client.post(
            "/api/auth/login", json={"password": ADMIN_PASSWORD},
            headers={**self.headers, "X-Forwarded-Proto": "https", "X-Forwarded-For": "1.2.3.4"},
        )
        self.assertTrue(response.cookies["oui_session"]["secure"])


class LimiterTests(unittest.TestCase):
    def test_account_limit_survives_restart_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.db"
            limiter = LoginLimiter(path)
            with patch("oui_wechat.auth.time.time", return_value=10000):
                for i in range(5):
                    limiter.check(f"peer-{i}", "owui:user@example.com")
                with self.assertRaises(RateLimited):
                    limiter.check("another-peer", "owui:user@example.com")
                limiter.close()
                limiter = LoginLimiter(path)
                with self.assertRaises(RateLimited):
                    limiter.check("another-peer", "owui:user@example.com")
            with patch("oui_wechat.auth.time.time", return_value=10901):
                limiter.check("another-peer", "owui:user@example.com")
            limiter.close()
            self.assertNotIn(b"user@example.com", path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ip_and_global_limits_cover_rotated_identities(self):
        for count, peer_for in ((20, lambda i: "same-peer"), (60, lambda i: f"peer-{i}")):
            with tempfile.TemporaryDirectory() as directory:
                limiter = LoginLimiter(Path(directory) / "attempts.db")
                with patch("oui_wechat.auth.time.time", return_value=10000):
                    for i in range(count):
                        limiter.check(peer_for(i), f"user-{i}")
                    with self.assertRaises(RateLimited):
                        limiter.check(peer_for(count), "next-user")
                limiter.close()

    def test_proxy_chain_ignores_spoofed_headers_and_uses_nearest_untrusted_hop(self):
        with patch.dict(os.environ, {"OUI_TRUSTED_PROXIES": "10.0.0.0/24,127.0.0.1/32"}):
            policy = ProxyPolicy()
        request = SimpleNamespace(
            remote="192.0.2.9", headers={"X-Forwarded-For": "1.1.1.1"}, secure=False
        )
        self.assertEqual(policy.peer(request), "192.0.2.9")
        request.remote = "127.0.0.1"
        request.headers["X-Forwarded-For"] = "1.1.1.1, 192.0.2.9, 10.0.0.2"
        self.assertEqual(policy.peer(request), "192.0.2.9")
        request.headers["X-Forwarded-For"] = "invalid"
        self.assertEqual(policy.peer(request), "127.0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
