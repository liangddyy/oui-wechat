# OUI WeChat

把个人微信接入 Open WebUI，通过 Web 管理页完成扫码、账号绑定和服务配置。
微信对话保存到对应用户的 Open WebUI 会话，继续使用该用户的模型权限和记忆。

![Web 管理界面示例，使用演示账号](docs/images/management.png)

## 功能

- Web 管理：首次设置管理密码，之后在浏览器中管理账号、服务状态、显示选项和日志。
- 微信授权：网页显示二维码、自动更新过期二维码，支持在网页输入数字配对码。
- 多用户：每名微信使用者独立扫码，分别绑定自己的 Open WebUI 账号。
- 真实会话：微信与其他客户端共享 Open WebUI 历史，每轮读取当前分支后续聊。
- 长期记忆：按 Open WebUI 的用户设置传递记忆开关，使用后端的记忆读取和写入能力。
- 默认显示思考状态提示，默认不发送思考正文；微信“正在输入”、工具状态、引用可分别配置。
- Web 绑定仅保存 JWT，不保存 Open WebUI 密码。令牌失效后可在网页更新登录。
- Docker 单服务部署，附带依赖锁和适配器源码，不依赖运行时 CDN 或额外克隆。

## Docker 部署

在仓库目录运行：

```sh
docker compose up -d --build
```

打开 `http://服务器地址:8787`，设置管理密码。
在“服务设置”填入 Open WebUI 地址，在“微信账号”添加微信并绑定 Open WebUI 账号。
之后的日常管理不需要命令行。飞牛可将仓库作为 Compose 项目导入后构建启动。

默认创建名为 `oui-data` 的持久数据卷，保存配置、微信授权、JWT 和管理密码哈希。
端口可通过 `.env` 中的 `OUI_PORT` 调整，完整部署说明见 [DEPLOY.md](DEPLOY.md)。

## 运行条件

- 微信有“设置 → 插件 → 微信 ClawBot”授权入口。每名使用者分别扫码授权。
- Open WebUI 开启密码登录，绑定的普通用户拥有可用模型及所需功能权限。
- 运行服务的机器能访问 Open WebUI 和微信 iLink。
- Open WebUI 地址必须能被适配器访问；容器中的 `127.0.0.1` 指向容器自身。

## 开发运行

需要 Python 3.11+ 和 uv：

```sh
uv sync --frozen
uv run oui-wechat --host 127.0.0.1 --port 8787
```

本地配置与凭据位于 `runtime/`，已从 Git 中排除。
打开 `http://127.0.0.1:8787` 使用同一管理界面。

```sh
uv run python scripts/validate.py --label local
uv run ruff check oui_wechat tests scripts validation
```

验证不需要真实微信或 Open WebUI 账号。另有开发探针可对真实实例进行隔离验收，
凭据从标准输入传入，不应写入命令参数、源代码或报告。

## 能力边界

- 已在 Open WebUI **0.12.0** 验证真实聊天保存、跨客户端续聊和记忆读写。
- 默认只处理微信私聊文字，不支持群聊，暂不投递图片或视频。
- 思考提示依赖模型返回相应事件；不是每个模型或每轮对话都会出现。
- “正在输入”由微信客户端呈现，发送开关不代表所有客户端一定显示。
- 网页与微信同时向同一会话发送消息仍存在竞态；服务会拒绝接续尚未完成的回复。
- 工具以模型默认配置和账号权限为准，依赖浏览器执行或审批的功能不能完整复用。
- 原版兼容的微信 `/login` 命令会存储密码；本项目的 Web 绑定流程不使用它。
- Web 登录令牌到期后需更新绑定，当前不保存密码做自动续签。
- iLink 与 Open WebUI 接口可能变化，版本升级后应重新验证。

验证范围与未完成的部署检查见 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 来源与许可

管理界面及新增服务代码采用 [MIT License](LICENSE)。
适配器基于 [Moha-Master/Open-WebUI-Weixin](https://github.com/Moha-Master/Open-WebUI-Weixin)，
附带于 `vendor/open-webui-weixin/`，保留作者信息、版本与本地修改记录。
第三方授权声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

提交前请保留 `.gitignore`，不要上传 `runtime/`、`.env`、日志、数据库或真实账号测试报告。
