"""Own the adapter lifecycle and browser-driven account operations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import httpx
from open_webui_weixin.adapter import Adapter
from open_webui_weixin.login import LoginFlow
from open_webui_weixin.main import build_client
from open_webui_weixin.owui import OwuiClient, SessionInfo
from open_webui_weixin.state import StateStore

from .adapter import WebAdapter
from .auth import Principal
from .config import ConfigStore
from .image_inbox import ImageInbox
from .media import qr_png

log = logging.getLogger(__name__)


@dataclass
class Pairing:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "creating"
    image: bytes = b""
    revision: int = 0
    account_id: str | None = None
    error: str | None = None
    task: asyncio.Task | None = None
    code: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    created: float = field(default_factory=time.time)
    principal: Principal | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "id": self.id, "status": self.status, "revision": self.revision,
            "account_id": self.account_id, "error": self.error,
            "image_url": f"/api/pairings/{self.id}/qr?v={self.revision}" if self.image else None,
        }


class ServiceManager:
    def __init__(self, config: ConfigStore):
        self.config = config
        self.state = StateStore(config.cfg.state_path)
        self.owui = OwuiClient(config.cfg.owui.base_url)
        self.media = ImageInbox(config.directory / "media", config.media)
        self.adapter: Adapter | None = None
        self.task: asyncio.Task | None = None
        self.login_client = None
        self.lock = asyncio.Lock()
        self.access_lock = asyncio.Lock()
        self.jobs: dict[str, Pairing] = {}
        self.last_error: str | None = None
        self.health_cache: tuple[float, dict] = (0, {})
        # Existing installations inherit ownership from their saved bindings once.
        for binding in self.state.list_bindings():
            uid = binding["wechat_user_id"]
            if not self.state.get_meta(f"web_owner:{uid}") and binding["owui_user_id"]:
                self._set_owner(uid, binding["owui_user_id"])

    @property
    def enabled(self) -> bool:
        return self.state.get_meta("web_service_enabled", "1") == "1"

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def _start(self) -> None:
        if self.running or not self.enabled or not self.state.load_accounts():
            return
        self.last_error = None
        self.login_client = build_client(self.config.cfg)
        self.adapter = WebAdapter(
            self.config.cfg, self.state, self.login_client, self.owui, access_lock=self.access_lock,
            media=self.media,
        )
        self.task = asyncio.create_task(self.adapter.run())
        self.task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            self.last_error = "适配器异常停止，请查看运行日志"
            log.error("适配器异常停止：%s", type(task.exception()).__name__)

    async def start(self) -> None:
        async with self.lock:
            self.state.set_meta("web_service_enabled", "1")
            await self._start()

    async def _stop(self) -> None:
        if self.adapter:
            self.adapter.request_stop()
        if self.task and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=15)
            except TimeoutError:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
            except Exception as exc:
                log.warning("停止时清理失败的适配器任务：%s", type(exc).__name__)
        if self.login_client:
            await self.login_client.close()
        self.task = None
        self.adapter = None
        self.login_client = None
        await self.media.detach()

    async def stop(self) -> None:
        async with self.lock:
            self.state.set_meta("web_service_enabled", "0")
            await self._stop()

    async def reconcile(self) -> None:
        self.media.start_cleanup()
        async with self.lock:
            if not self.state.load_accounts():
                await self._stop()
            elif self.running:
                await self.adapter._sync_accounts_once()
            else:
                await self._start()

    async def apply_settings(self, incoming: dict) -> None:
        raw = self.config.merge(incoming)
        if raw["owui"]["base_url"] != self.config.cfg.owui.base_url and self.state.list_bindings():
            raise ValueError("修改 Open WebUI 地址前，请先解除已有账号绑定")
        async with self.lock:
            await self._stop()
            self.config.write(raw)
            self.media.settings = self.config.media
            self.media.store.settings = self.config.media
            self.media.weixin.settings = self.config.media
            if not self.config.media.enabled:
                for row in self.media.store.db.execute("SELECT DISTINCT uid FROM batches").fetchall():
                    self.media.clear(row["uid"])
            await self.owui.close()
            self.owui = OwuiClient(self.config.cfg.owui.base_url)
            self.health_cache = (0, {})
            await self._start()
        log.info("管理页更新设置，适配器已按新配置重载")

    def account(self, account_id: str):
        for row in self.state.load_accounts():
            if row["account_id"] == account_id:
                return row
        raise ValueError("微信连接不存在，请刷新列表")

    def _set_owner(self, uid: str, user_id: str) -> None:
        self.state.set_meta(f"web_owner:{uid}", json.dumps({
            "user_id": user_id, "base_url": self.config.cfg.owui.base_url,
        }))

    def owns(self, uid: str, principal: Principal) -> bool:
        value = self.state.get_meta(f"web_owner:{uid}")
        owner = json.loads(value) if value else {}
        return bool(
            principal.owui and principal.active
            and principal.base_url == self.config.cfg.owui.base_url == owner.get("base_url")
            and principal.owui.user_id == owner.get("user_id")
        )

    def allowed_account(self, account_id: str, principal: Principal) -> bool:
        return any(
            row["account_id"] == account_id
            and (principal.role == "admin" or self.owns(row["scanner_user_id"], principal))
            for row in self.state.load_accounts()
        )

    def accounts(self, principal: Principal | None = None) -> list[dict]:
        result = []
        for index, row in enumerate(self.state.load_accounts(), 1):
            uid = row["scanner_user_id"]
            if principal and principal.role != "admin" and not self.owns(uid, principal):
                continue
            binding = self.state.get_binding(uid) if uid else None
            focus = self.state.get_focus(uid) if uid else None
            handle = self.adapter._accounts.get(row["account_id"]) if self.adapter else None
            connected = bool(handle and handle.token and handle.task and not handle.task.done())
            expiry = binding["jwt_expires_at"] if binding else None
            alias = self.state.get_meta(f"alias:{row['account_id']}")
            result.append({
                "id": row["account_id"], "name": alias or (binding["owui_name"] if binding else "")
                or f"微信账号 {index}",
                "bound": binding is not None,
                "email": binding["owui_email"] if binding else None,
                "owui_name": binding["owui_name"] if binding else None,
                "connected": connected,
                "expired": bool(expiry and expiry <= time.time()),
                "expires_at": expiry,
                "model_id": focus["model_id"] if focus else None,
                "chat_title": focus["chat_title"] if focus else None,
                "created_at": row["created_at"],
            })
        return result

    async def health(self, force: bool = False) -> dict:
        if not force and self.health_cache[0] > time.monotonic() - 5:
            return self.health_cache[1]
        value = {"connected": False, "version": None}
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                base = self.config.cfg.owui.base_url
                health = await client.get(f"{base}/health")
                value["connected"] = health.status_code == 200
                response = await client.get(f"{base}/api/version")
                if response.is_success:
                    value["version"] = response.json().get("version")
        except (httpx.HTTPError, ValueError):
            pass
        self.health_cache = (time.monotonic(), value)
        return value

    async def _drop_runtime(self, uid: str) -> None:
        self.media.clear(uid)
        epoch = int(self.state.get_meta(f"media_epoch:{uid}", "0")) + 1
        self.state.set_meta(f"media_epoch:{uid}", str(epoch))
        if self.adapter:
            await self.adapter.stop_current(uid)
            await self.adapter.runtimes.drop_socket(uid)

    def _reset_focus(self, uid: str) -> None:
        self.state.set_focus(
            uid, chat_id=None, leaf_id=None, model_id=None,
            chat_title=None, temporary=0, is_first_message=True,
        )
        self.state.temporary_drop(uid)
        self.state.save_snapshot(uid, "chats", [])
        self.state.save_snapshot(uid, "models", [])
        self.state.take_pending(uid)

    async def bind(self, account_id: str, email: str, password: str) -> None:
        row = self.account(account_id)
        uid = row["scanner_user_id"]
        if not uid:
            raise ValueError("扫码者身份缺失，请重新授权微信")
        if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
            raise ValueError("请输入 Open WebUI 邮箱和密码")
        if len(email) > 320 or len(password) > 1024:
            raise ValueError("账号或密码长度无效")
        session = await self.owui.signin(email.strip(), password)
        self.validate_session(session)
        await self.bind_session(account_id, session)

    @staticmethod
    def validate_session(session: SessionInfo) -> None:
        if (
            not session.user_id or not session.jwt_token or session.role not in {"admin", "user"}
            or session.expires_at is not None and session.expires_at <= time.time()
        ):
            raise ValueError("Open WebUI 登录不可用，请确认账号已获准使用")

    async def bind_session(self, account_id: str, session: SessionInfo) -> None:
        uid = self.account(account_id)["scanner_user_id"]
        if not uid:
            raise ValueError("扫码者身份缺失，请重新授权微信")
        self.validate_session(session)
        resolved = await self.owui.resolve_default_model(session.jwt_token)
        await self._drop_runtime(uid)
        self._store_binding(uid, session, resolved)
        log.info("已通过管理页绑定一个 Open WebUI 用户，未保存密码")

    def _store_binding(self, uid: str, session: SessionInfo, resolved: tuple[str, str] | None) -> None:
        previous = self.state.get_binding(uid)
        if not previous or previous["owui_user_id"] != session.user_id:
            self._reset_focus(uid)
        self.state.upsert_binding(uid, {
            "email": session.email, "password": "",
            "owui_user_id": session.user_id, "owui_name": session.name,
            "jwt_token": session.jwt_token, "jwt_expires_at": session.expires_at,
        })
        self._set_owner(uid, session.user_id)
        focus = self.state.get_focus(uid)
        if resolved and (not focus or not focus["model_id"]):
            self.state.set_focus(uid, model_id=resolved[0])

    async def unbind(self, account_id: str) -> None:
        uid = self.account(account_id)["scanner_user_id"]
        await self._drop_runtime(uid)
        self.state.delete_binding(uid)
        self._reset_focus(uid)
        log.info("已解除一个账号的 Open WebUI 绑定")

    async def remove(self, account_id: str) -> None:
        await self.unbind(account_id)
        self.state.clear_account(account_id)
        await self.reconcile()

    def rename(self, account_id: str, name: str) -> None:
        self.account(account_id)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError("账号名称需为 1–40 个字符")
        self.state.set_meta(f"alias:{account_id}", name.strip())

    async def models(self, account_id: str) -> list[dict]:
        row = self.account(account_id)
        binding = self.state.get_binding(row["scanner_user_id"])
        if not binding:
            raise ValueError("请先绑定 Open WebUI 账号")
        models = await self.owui.list_models(binding["jwt_token"])
        result = []
        for model in models:
            info = model.get("info")
            info = info if isinstance(info, dict) else {}
            meta = info.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            if model.get("id") and not meta.get("hidden"):
                result.append({
                    "id": model["id"],
                    "name": info.get("name") or model.get("name") or model["id"],
                })
        return result

    async def set_model(self, account_id: str, model_id: str) -> None:
        available = await self.models(account_id)
        if not any(model["id"] == model_id for model in available):
            raise ValueError("该账号没有使用此模型的权限")
        uid = self.account(account_id)["scanner_user_id"]
        self.state.set_focus(uid, model_id=model_id)

    async def new_chat(self, account_id: str) -> None:
        uid = self.account(account_id)["scanner_user_id"]
        await self._drop_runtime(uid)
        self.state.clear_focus_chat(uid)
        self.state.set_focus(uid, temporary=0)
        self.state.temporary_drop(uid)

    async def verify_binding(self, account_id: str) -> dict:
        uid = self.account(account_id)["scanner_user_id"]
        binding = self.state.get_binding(uid)
        if not binding:
            raise ValueError("请先绑定 Open WebUI 账号")
        session = await self.owui.whoami(binding["jwt_token"])
        return {"valid": session.user_id == binding["owui_user_id"]}

    async def add_pairing(self, principal: Principal | None = None) -> Pairing:
        if sum(bool(job.task and not job.task.done()) for job in self.jobs.values()) >= 5:
            raise ValueError("请先完成或取消已有的扫码")
        if principal and principal.role == "user" and any(
            job.principal and job.principal.owui
            and job.principal.owui.user_id == principal.owui.user_id
            and job.task and not job.task.done() for job in self.jobs.values()
        ):
            raise ValueError("请先完成或取消自己的扫码")
        self.jobs = {
            key: job for key, job in self.jobs.items()
            if job.task and not job.task.done() or job.created > time.time() - 600
        }
        if len(self.jobs) >= 200:
            raise ValueError("扫码请求过多，请稍后重试")
        job = Pairing(principal=principal)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._pair(job))
        return job

    async def _pair(self, job: Pairing) -> None:
        client = build_client(self.config.cfg)

        async def on_qr(content: str) -> None:
            job.image = qr_png(content)
            job.revision += 1
            job.status = "wait"

        async def on_status(value: str) -> None:
            job.status = value

        async def get_code() -> str:
            job.status = "need_verifycode"
            return await asyncio.wait_for(job.code.get(), timeout=180)

        async def on_confirm(result: dict) -> None:
            # LoginFlow calls this before writing anything, so rejected scans cannot replace tokens.
            principal = job.principal
            if principal and not principal.active:
                raise ValueError("登录已过期，请重新登录")
            session = None
            base_url = self.config.cfg.owui.base_url
            if principal and principal.role == "user":
                if not self.user_session_active(principal):
                    raise ValueError("登录已过期，请重新登录后扫码")
                session = await self.owui.whoami(principal.owui.jwt_token)
                self.validate_session(session)
                if session.user_id != principal.owui.user_id:
                    raise ValueError("账号身份已变化，请重新登录")
            async with self.access_lock:
                if principal and not principal.active:
                    raise ValueError("登录已过期，请重新登录")
                uid = result["scanner_user_id"]
                if not uid or not result["bot_id"]:
                    raise ValueError("微信授权身份缺失")
                if session:
                    if not self.user_session_active(principal) or base_url != self.config.cfg.owui.base_url:
                        raise ValueError("登录配置已变化，请重新登录")
                    owner = self.state.get_meta(f"web_owner:{uid}")
                    if owner and not self.owns(uid, principal):
                        raise ValueError("此微信已属于其他用户，请联系管理员处理")
                    existing = next((
                        row for row in self.state.load_accounts()
                        if row["account_id"] == result["bot_id"]
                    ), None)
                    if existing and existing["scanner_user_id"] != uid:
                        raise ValueError("此微信连接已被使用，请联系管理员处理")
                    # Resolve before saving the new authorization; no partly bound user account.
                    resolved = await self.owui.resolve_default_model(session.jwt_token)
                    if not self.user_session_active(principal):
                        raise ValueError("登录已过期，请重新登录")
                    await self._drop_runtime(uid)
                    if not self.user_session_active(principal):
                        raise ValueError("登录已过期，请重新登录")
                    self._store_binding(uid, session, resolved)
                self.state.save_login(result)

        try:
            flow = LoginFlow(
                client, self.state, self.config.cfg.weixin.bot_type,
                on_qr=on_qr, on_status=on_status, get_code=get_code,
                on_confirm=on_confirm,
            )
            result = await asyncio.wait_for(flow.run(), timeout=300)
            job.account_id = result["bot_id"]
            job.status = "confirmed"
            job.image = b""
            await self.reconcile()
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.image = b""
            raise
        except ValueError as exc:
            job.status = "error"
            job.error = str(exc)
        except Exception as exc:
            job.status = "error"
            job.error = "微信授权失败，请刷新二维码重试"
            log.warning("微信扫码未完成：%s", type(exc).__name__)
        finally:
            job.image = b""
            job.principal = self._pairing_identity(job.principal)
            await client.close()

    @staticmethod
    def _pairing_identity(principal: Principal | None) -> Principal | None:
        """Retain task ownership without retaining a logged-out user's bearer token."""
        if not principal or not principal.owui:
            return principal
        session = principal.owui
        return Principal("user", float("inf"), SessionInfo(
            "", None, session.user_id, "", "", "user"
        ), principal.base_url)

    def user_session_active(self, principal: Principal) -> bool:
        return (
            principal.active and self.config.user_login
            and principal.base_url == self.config.cfg.owui.base_url
        )

    async def cancel_pairing(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job and job.task and not job.task.done():
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task

    async def close(self) -> None:
        for job in list(self.jobs.values()):
            await self.cancel_pairing(job.id)
        async with self.lock:
            await self._stop()
        await self.owui.close()
        await self.media.close()
        self.state.close()
