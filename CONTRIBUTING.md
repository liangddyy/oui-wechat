# 参与开发

使用 `uv sync --frozen` 安装开发依赖，使用 `uv run oui-wechat` 启动本地管理页。

提交前运行：

```sh
uv run python scripts/validate.py --label local
uv run ruff check oui_wechat tests scripts validation
```

修改页面后检查桌面和移动端的登录、空账号列表、扫码、绑定、设置和错误状态。
调整适配器接口时，应增加真实协议形状的回归测试，并更新 `UPSTREAM.json` 的修改说明。

浏览器回归使用独立、全新的数据目录启动服务：

```sh
uv run oui-wechat --port 8790 --data-dir reports/browser-runtime
npm ci
npx playwright install chromium --only-shell
npm run test:browser
```

测试会在该目录创建一次性的管理密码，不能指向已有账号的正式管理服务。
外部微信和 Open WebUI 交互在浏览器测试中使用明确的接口测试替身。

不要在测试、截图、日志或示例中包含真实 JWT、密码、微信令牌、个人邮箱或部署域名。
外部微信和 Open WebUI 的认证验收使用临时账号，默认单元测试不得连接这些服务。
