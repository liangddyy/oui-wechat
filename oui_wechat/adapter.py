"""The hosted service manages credentials in the browser, including legacy commands."""

from open_webui_weixin.adapter import Adapter
from open_webui_weixin.commands import CommandHandler


class WebCommands(CommandHandler):
    async def handle(self, wechat_user_id: str, text: str) -> str | None:
        name = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
        if name in {"/login", "/login-refresh", "/logout", "/relogin", "/ldap"}:
            return "请在 OUI WeChat 网页登录并管理微信绑定。"
        reply = await super().handle(wechat_user_id, text)
        if reply:
            reply = reply.replace("`/login <邮箱> <密码>`", "OUI WeChat 网页")
            reply = reply.replace("`/login-refresh`", "OUI WeChat 网页更新登录")
        return reply


class WebAdapter(Adapter):
    def __init__(self, *args, access_lock):
        super().__init__(*args)
        self.access_lock = access_lock
        self.commands = WebCommands(
            self.cfg, self.state, self.owui, self.commands.ctx, login_flow=None
        )

    async def _dispatch(self, handle, msg) -> None:
        # Account changes can await cancellation/socket teardown; do not enqueue with the old JWT.
        async with self.access_lock:
            await super()._dispatch(handle, msg)

    async def _enqueue_chat(self, wechat_user_id: str, text: str) -> None:
        if not self.state.get_binding(wechat_user_id):
            await self.send_text(wechat_user_id, "请在 OUI WeChat 网页登录并绑定微信。")
            return
        await super()._enqueue_chat(wechat_user_id, text)
