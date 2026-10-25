# 开发约定

- 始终使用中文与用户交流。
- `oui_wechat/` 是 Web 管理层；`vendor/open-webui-weixin/` 是附带的适配器。
- 日常管理必须可以在网页完成，不要求用户在微信发送密码或使用命令行绑定。
- 默认启用思考提示，默认不发送思考正文。
- 不返回或记录 JWT、微信令牌、密码和完整二维码内容。
- `runtime/`、`reports/`、`.env`、构建包和本地上游副本不得加入 Git。
- 保留上游来源、版本、授权声明和本地修改说明。
- 验证：`uv run python scripts/validate.py --label local` 和 `uv run ruff check oui_wechat tests scripts validation`。
- 修改管理界面后用浏览器验证桌面、移动端、登录、空状态和主要弹窗。
