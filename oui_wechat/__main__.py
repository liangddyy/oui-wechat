from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from aiohttp import web

from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="OUI WeChat Web 管理服务")
    parser.add_argument("--host", default=os.environ.get("OUI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OUI_PORT", "8787")))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("OUI_DATA_DIR", "runtime")))
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(create_app(args.data_dir), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
