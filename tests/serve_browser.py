"""Isolated browser fixture: real web/auth APIs, fake external Open WebUI and WeChat."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from test_web import FakeIlink, TestManager

from oui_wechat.web import create_app

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="oui-browser-") as directory, patch(
        "oui_wechat.service.build_client",
        side_effect=lambda cfg: FakeIlink(require_code=True, name="browser"),
    ):
        web.run_app(
            create_app(Path(directory), manager_factory=TestManager),
            host="127.0.0.1", port=8790, access_log=None,
        )
