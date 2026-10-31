"""Browser-managed identities and per-user, image-first message queues."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from open_webui_weixin.adapter import Adapter, extract_text
from open_webui_weixin.chat import ChatRunner
from open_webui_weixin.commands import CommandHandler
from open_webui_weixin.runtime import PENDING_LIMIT
from open_webui_weixin.weixin_protocol import ITEM_IMAGE, MESSAGE_TYPE_USER, SessionExpiredError

from .image_chat import ImageChatRunner, require_vision
from .image_inbox import IncomingTurn
from .image_transport import MediaError
from .logs import redact

log = logging.getLogger(__name__)
IMAGE_BUSY_NOTICE = (
    "正在生成或发送图片，本条消息未接收，也不会排队。"
    "请等待完成后重新发送；如需中止，请发送 `/stop`。"
)


class WebCommands(CommandHandler):
    def __init__(self, *args, invalidate=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.invalidate = invalidate

    async def handle(self, wechat_user_id: str, text: str) -> str | None:
        name = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
        if name in {"/login", "/login-refresh", "/logout", "/relogin", "/ldap"}:
            return "请在 OUI WeChat 网页登录并管理微信绑定。"
        focus = self.state.get_focus(wechat_user_id)
        before = (focus["chat_id"], focus["temporary"]) if focus else (None, 0)
        reply = await super().handle(wechat_user_id, text)
        focus = self.state.get_focus(wechat_user_id)
        after = (focus["chat_id"], focus["temporary"]) if focus else (None, 0)
        restarted = bool(reply and (
            reply.startswith("**已进入新会话。**")
            or reply.startswith("**已清空临时聊天记录")
        ))
        if self.invalidate and (before != after or restarted):
            self.invalidate(wechat_user_id)
        if reply:
            reply = reply.replace("`/login <邮箱> <密码>`", "OUI WeChat 网页")
            reply = reply.replace("`/login-refresh`", "OUI WeChat 网页更新登录")
        return reply


class WebAdapter(Adapter):
    def __init__(self, *args, access_lock, media=None):
        super().__init__(*args)
        self.access_lock = access_lock
        self.media = media
        self._image_generating: set[str] = set()
        self.commands = WebCommands(
            self.cfg, self.state, self.owui, self.commands.ctx, login_flow=None,
            invalidate=self.invalidate,
        )

    def scope(self, uid: str, account_id: str | None = None) -> str:
        binding = self.state.get_binding(uid)
        account_id = account_id or self._account_id_for(uid)
        if not binding or not account_id or not any(
            row["account_id"] == account_id and row["scanner_user_id"] == uid
            for row in self.state.load_accounts()
        ):
            return ""
        return json.dumps({
            "account": account_id, "owner": binding["owui_user_id"], "backend": self.cfg.owui.base_url,
            "epoch": self.state.get_meta(f"media_epoch:{uid}", "0"),
        }, sort_keys=True)

    def invalidate(self, uid: str):
        epoch = int(self.state.get_meta(f"media_epoch:{uid}", "0")) + 1
        self.state.set_meta(f"media_epoch:{uid}", str(epoch))
        if self.media:
            self.media.clear(uid)
        rt = self.runtimes.get(uid)
        for turn in rt.pending:
            if isinstance(turn, IncomingTurn) and turn.batch_id and self.media:
                self.media.store.drop(turn.batch_id)
        rt.pending.clear()
        rt.stopped = True
        if rt.current_task and rt.current_task is not asyncio.current_task():
            rt.current_task.cancel()

    async def run(self):
        if self.media:
            self.media.attach(self)
        try:
            await super().run()
        finally:
            if self.media:
                await self.media.detach()

    async def _dispatch(self, handle, msg) -> None:
        replies = []
        async with self.access_lock:
            if int(msg.get("message_type") or 0) != MESSAGE_TYPE_USER:
                await super()._dispatch(handle, msg)
                return
            uid = str(msg.get("from_user_id") or "").strip()
            if not uid:
                return
            if msg.get("context_token"):
                self.state.save_context_token(handle.account_id, uid, msg["context_token"])
            self._user_account[uid] = handle.account_id
            if self.media and self.media.store.seen(handle.account_id, msg):
                return
            text = extract_text(msg)
            items = [item for item in msg.get("item_list") or []
                     if isinstance(item, dict) and item.get("type") == ITEM_IMAGE]
            scope = self.scope(uid, handle.account_id)
            if not scope:
                replies.append("请在 OUI WeChat 网页登录并绑定微信。")
            elif self.image_busy(uid) and text.strip().lower() != "/stop":
                replies.append(IMAGE_BUSY_NOTICE)
            elif text.lstrip().startswith("/"):
                reply = await self.commands.handle(uid, text)
                if reply:
                    replies.append(reply)
            else:
                if items:
                    if not self.media or not self.media.settings.enabled:
                        replies.append("管理员尚未开启图片收发。")
                        text = ""  # Never submit a question while silently dropping its images.
                    else:
                        try:
                            _, first, ids = self.media.store.add(uid, scope, items)
                            for image_id in ids:
                                self.media.ensure_download(image_id)
                            if first and not text:
                                replies.append("已收到图片，请继续发送问题。")
                        except MediaError as exc:
                            replies.append(str(exc))
                            text = ""
                if text:
                    notice = self.queue_turn(uid, text, scope)
                    if notice:
                        replies.append(notice)
        # No image download/upload or outbound HTTP runs while holding the account lock.
        for reply in replies:
            await self.send_text(uid, reply)
        await self._maybe_hint_jwt_expiry(uid)

    async def _enqueue_chat(self, wechat_user_id: str, text: str) -> None:
        scope = self.scope(wechat_user_id)
        if not scope:
            await self.send_text(wechat_user_id, "请在 OUI WeChat 网页登录并绑定微信。")
            return
        notice = self.queue_turn(wechat_user_id, text, scope)
        if notice:
            await self.send_text(wechat_user_id, notice)

    def queue_turn(self, uid: str, text: str, scope: str) -> str | None:
        rt = self.runtimes.get(uid)
        if self.image_busy(uid):
            return IMAGE_BUSY_NOTICE
        if rt.stopped and rt.busy:
            return "正在切换会话，请稍后重新发送问题。"
        if len(rt.pending) >= PENDING_LIMIT:
            return "待处理问题过多，请等待已有回复完成。"
        batch = self.media.store.claim(uid, scope) if self.media and self.media.settings.enabled else None
        if (
            not batch and rt.pending and not rt.pending[-1].batch_id
            and rt.pending[-1].scope == scope
        ):
            previous = rt.pending[-1]
            rt.pending[-1] = IncomingTurn(previous.text + "\n\n" + text, scope)
        else:
            rt.pending.append(IncomingTurn(text, scope, batch))
        rt.account_id = json.loads(scope)["account"]
        if rt.busy:
            return f"已排入队列，当前第 {len(rt.pending)} 条待处理。"
        rt.stopped = False
        self._set_image_generating(uid, self.cfg.capabilities.image_generation)
        # No debounce/timer on the text path.
        rt.current_task = asyncio.create_task(self._worker(rt, ""))
        return None

    def image_busy(self, uid: str) -> bool:
        return uid in self._image_generating and self.runtimes.get(uid).busy

    def _set_image_generating(self, uid: str, enabled: bool):
        if enabled:
            self._image_generating.add(uid)
        else:
            self._image_generating.discard(uid)

    async def _check_vision(self, uid: str, token: str):
        focus = self.state.get_focus(uid)
        model_id = focus["model_id"] if focus else None
        if not model_id:
            resolved = await self.owui.resolve_default_model(token)
            if not resolved:
                raise MediaError("当前账号没有可用模型")
            model_id = resolved[0]
            self.state.set_focus(uid, model_id=model_id)
        await require_vision(self.owui, token, model_id)

    async def _worker(self, rt, jwt_token):
        uid = rt.wechat_user_id
        try:
            while rt.pending and not rt.stopped:
                turn = rt.pending.pop(0)
                if self.scope(uid) != turn.scope:
                    if turn.batch_id and self.media:
                        self.media.store.drop(turn.batch_id)
                    continue
                binding = self.state.get_binding(uid)
                token = binding["jwt_token"]
                result, files = None, None
                # Protect the preflight window until the actual model capabilities are known.
                self._set_image_generating(uid, self.cfg.capabilities.image_generation)
                try:
                    if turn.batch_id:
                        await self._check_vision(uid, token)
                        focus = self.state.get_focus(uid)
                        files = await self.media.prepare(
                            turn.batch_id, uid, turn.scope, token, bool(focus and focus["temporary"]),
                        )
                    if self.scope(uid) != turn.scope:
                        continue
                    socket = await self.runtimes.ensure_socket(uid, token)
                    if self.scope(uid) != turn.scope:
                        continue
                    runner_args = (self.cfg, self.state, self.owui, socket, self.send_text)
                    runner_kwargs = {
                        "generation_state": lambda enabled: self._set_image_generating(uid, enabled),
                    }
                    if self.media and self.media.settings.enabled:
                        runner = ImageChatRunner(
                            *runner_args, inbox=self.media, scope=turn.scope, send_image=self.send_image,
                            batch_id=turn.batch_id,
                            **runner_kwargs,
                        )
                    else:
                        runner = ChatRunner(*runner_args, **runner_kwargs)
                    await self._typing_start(rt.account_id, uid)
                    async with rt.lock:
                        result = await runner.run_turn(uid, token, turn.text, files=files)
                    if result.error:
                        await self.send_text(uid, redact(str(result.error))[:1024])
                    elif result.sent_segments == 0:
                        await self.send_text(uid, "本轮没有可显示的回复，请在 OpenWebUI 查看。")
                except MediaError as exc:
                    if self.scope(uid) == turn.scope:
                        await self.send_text(uid, str(exc))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("聊天或图片处理未完成：%s", type(exc).__name__)
                    if self.scope(uid) == turn.scope:
                        await self.send_text(uid, "本轮处理未完成，请重试或在 OpenWebUI 查看。")
                finally:
                    self._image_generating.discard(uid)
                    await self._typing_stop(rt.account_id, uid)
                    if turn.batch_id and self.media:
                        if result and result.accepted or self.scope(uid) != turn.scope:
                            self.media.store.drop(turn.batch_id)
                        else:
                            self.media.store.retry(turn.batch_id)
        finally:
            if rt.current_task is asyncio.current_task():
                rt.current_task = None
                self._image_generating.discard(uid)

    async def send_image(self, uid: str, scope: str, image):
        if self.scope(uid) != scope:
            raise MediaError("图片所属账号已变化")
        handle = self._accounts.get(json.loads(scope)["account"])
        if not handle or not handle.token:
            raise MediaError("微信连接已断开")
        try:
            item = await self.media.weixin.upload(handle.client, handle.token, uid, image)
            if self.scope(uid) != scope:
                raise MediaError("图片所属账号已变化")
            context = self.state.get_context_token(handle.account_id, uid)
            if not context:
                raise MediaError("微信回复凭据已过期，请重新发消息")
            await handle.client.send_message(handle.token, uid, [item], context)
        except SessionExpiredError:
            await self._account_expired(handle)
            raise MediaError("微信授权已过期，请重新扫码") from None

    async def stop_current(self, uid: str) -> str:
        self.invalidate(uid)
        rt = self.runtimes.get(uid)
        if rt.current_task and rt.current_task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await rt.current_task
        return await super().stop_current(uid)
