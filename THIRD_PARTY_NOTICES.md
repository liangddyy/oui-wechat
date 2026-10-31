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

## Tencent openclaw-weixin

- 作者：Tencent
- 协议参考包：`@tencent-weixin/openclaw-weixin`，固定版本 `2.4.10`
- 来源：https://registry.npmjs.org/@tencent-weixin/openclaw-weixin/-/openclaw-weixin-2.4.10.tgz
- 授权：MIT；媒体协议字段、AES 编码、上传及图片消息流程参照其源码。
- 本项目的 Python 媒体实现位于 `oui_wechat/`，不附带该包的其他功能。

保留参考包 LICENSE 的版权及许可声明：

```text
Tencent is pleased to support the open source community by making openclaw-weixin available.

Copyright (C) 2026 Tencent. All rights reserved.

openclaw-weixin is licensed under the MIT.

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

媒体依赖 `cryptography`、Pillow 与 markdown-it-py 通过锁文件安装，许可证随各自发行包保留。
