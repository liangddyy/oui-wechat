# 参与开发

使用 `uv sync --frozen` 安装开发依赖，使用 `uv run oui-wechat` 启动本地管理页。

提交前运行：

```sh
uv run python scripts/validate.py --label local
uv run ruff check oui_wechat tests scripts validation
```

修改页面后检查桌面和移动端的登录、空账号列表、扫码、绑定、设置和错误状态。
调整适配器接口时，应增加真实协议形状的回归测试，并更新 `UPSTREAM.json` 的修改说明。

浏览器回归先启动隔离测试服务，再在另一个终端运行测试：

```sh
uv run python tests/serve_browser.py
```

```sh
npm ci
npx playwright install chromium --only-shell
npm run test:browser
```

测试服务使用临时目录和虚构账号，外部微信与 Open WebUI 交互使用接口替身。
不要把测试指向已有账号的正式管理服务。

不要在测试、截图、日志或示例中包含真实 JWT、密码、微信令牌、个人邮箱或部署域名。
外部微信和 Open WebUI 的认证验收使用临时账号，默认单元测试不得连接这些服务。

## 公开发布

发布前检查 `git status --short`、暂存区差异和 Git 历史，确认没有提交运行数据或凭据。
只通过 Git 发布源码，不要将整个工作目录或部署数据目录打包上传。
`runtime/`、`data/`、`media/`、`reports/`、`artifacts/`、环境配置、数据库、日志、私钥和备份属于私有文件。
示例配置使用占位地址，截图使用虚构账号；第三方来源与授权声明必须保留。
