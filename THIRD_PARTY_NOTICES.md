# 第三方来源

## Open-WebUI-Weixin

- 作者：Moha-Master
- 源码：https://github.com/Moha-Master/Open-WebUI-Weixin
- 版本：0.1.0
- 原始提交：`e30b2990163c317f09f96ae657e3fd88d1be61d0`
- 附带目录：`vendor/open-webui-weixin/`

上游在 `pyproject.toml` 中声明 `license = "MIT"`；固定提交未附带独立的 LICENSE 文件。
本仓库保留其项目元数据、README 和作者信息。本仓库根目录的 MIT 许可证适用于
OUI WeChat 的新增管理代码，不替代第三方原有授权声明。
来源和本地修正记录见该目录的 `UPSTREAM.json`。

主要修正包括 Socket.IO 会话标识、跨客户端续聊父节点同步、思考提示默认开启，
以及供 Web 管理调用的异步二维码与配对码接口。

## Lucide

- 项目：https://lucide.dev/
- 源码：https://github.com/lucide-icons/lucide
- 版本：`lucide-static 0.468.0`
- 授权：ISC；部分衍生图标另保留原有 MIT 声明。
- 附带目录：`oui_wechat/static/icons/`
- 原始许可证：`oui_wechat/static/icons/LICENSE`

只附带界面实际使用的 SVG 图标，不依赖第三方 CDN。
