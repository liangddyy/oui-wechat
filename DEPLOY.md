# 部署

## Docker / 飞牛

将整个仓库作为 Compose 项目导入，使用根目录的 `compose.yaml` 和 `Dockerfile`。
构建完成后访问宿主机的 `8787` 端口。首次打开管理页设置管理密码，再配置 Open WebUI、
扫码添加微信、绑定各自的 Open WebUI 账号。

默认数据保存在命名卷 `oui-data`。容器以非 root 用户运行，不需要额外创建配置文件或生成 UID/GID。

```sh
docker compose up -d --build
```

若需修改映射端口，在项目 `.env` 设置：

```dotenv
OUI_PORT=8787
```

若只允许本机反向代理访问，可增加：

```dotenv
OUI_BIND_ADDRESS=127.0.0.1
```

管理页可以暂停微信收发、添加或移除账号、更新 Open WebUI 登录、选择模型、新建会话、
调整显示和功能开关。保存设置时会重载适配器，不要求重新授权已有微信。

## Open WebUI 网络地址

- 域名地址：直接填写容器可访问的完整 `http(s)` 地址。
- 宿主机上的 Open WebUI：使用 `http://host.docker.internal:实际端口`；
  Compose 已配置对应宿主机映射。
- 同一 Docker 网络：可使用 Open WebUI 的服务名和容器端口。

已绑定账号时，修改后端地址前必须解除绑定，避免将已有令牌发送给另一个服务。

## 数据迁移

`/data` 中包括：

```text
config.yaml          适配器配置
admin.json           管理密码哈希
data/state.db        微信授权、用户 JWT、会话状态
```

备份与迁移整个数据卷；先停止服务，或使用 SQLite 备份机制生成一致性快照。
`data/` 中的 JWT 与微信令牌是运行必需凭据，不能上传到公开仓库。

若迁移已有本地 `runtime/`，可使用 Compose 覆盖文件绑定该目录，
并将容器用户设为对该目录有读写权限的 UID/GID。不要同时运行两个轮询相同微信账号的实例。

## 登录与反向代理

管理密码与 Open WebUI 密码相互独立。首次设置后，管理 API 需要有效管理会话。
Web 绑定只保留 JWT；其到期或被后端撤销后，在账号菜单中选择“更新登录”。

对公网提供管理页时使用 HTTPS。反向代理应保留原始 `Host`，
并正确设置 `X-Forwarded-Proto`，以便请求来源校验与 Secure Cookie 正常工作。
管理页包含账号与服务控制能力，应仅提供给可信的管理者。

## 启停与升级

```sh
docker compose down
docker compose up -d --build
```

停止不会删除数据卷。升级前备份数据卷，并核对目标 Open WebUI 版本的兼容性。
健康检查只表示管理服务存活，微信和 Open WebUI 的连接状态以管理页为准。
网页日志保留当前进程最近 300 条记录，重启后清空；Docker 标准输出日志另按 Compose 配置轮转。
