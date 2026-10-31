"""Read and atomically update the adapter's existing configuration format."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, load_config

BOOL_PATHS = (
    "display.reasoning.enable",
    "display.reasoning.detailed",
    "display.tool_status.enable",
    "display.tool_status.detailed",
    "display.typing",
    "display.citations",
    "capabilities.memory",
    "capabilities.tools",
    "capabilities.web_search",
    "capabilities.allow_terminal",
    "capabilities.image_generation",
    "media.enabled",
)


@dataclass(frozen=True)
class MediaConfig:
    enabled: bool = True
    pending_ttl_seconds: int = 120
    max_images_per_turn: int = 5
    max_image_bytes: int = 10 * 1024 * 1024
    max_batch_bytes: int = 25 * 1024 * 1024

    @classmethod
    def load(cls, raw: dict) -> MediaConfig:
        values = {**asdict(cls()), **raw}
        if type(values["enabled"]) is not bool:
            raise ValueError("图片收发开关必须为布尔值")
        limits = {
            "pending_ttl_seconds": (30, 600),
            "max_images_per_turn": (1, 10),
            "max_image_bytes": (1024 * 1024, 20 * 1024 * 1024),
            "max_batch_bytes": (1024 * 1024, 50 * 1024 * 1024),
        }
        for key, (minimum, maximum) in limits.items():
            value = values[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"图片设置 {key} 需为 {minimum}–{maximum} 之间的整数")
        if values["max_batch_bytes"] < values["max_image_bytes"]:
            raise ValueError("每批图片大小上限不能小于单张图片上限")
        return cls(**{key: values[key] for key in asdict(cls())})


def validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Open WebUI 地址无效")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise ValueError("请输入不含账号密码或查询参数的 http(s) 地址")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Open WebUI 端口无效") from exc
    return value.strip().rstrip("/")


class ConfigStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "config.yaml"
        if not self.path.exists():
            raw = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
            raw["display"]["reasoning"] = {"enable": True, "detailed": False}
            raw["capabilities"]["allow_terminal"] = False
            raw["capabilities"]["image_generation"] = False
            self.write(raw)
        self.cfg = load_config(self.path)

    def read(self) -> dict:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))

    def write(self, raw: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            yaml.safe_dump(raw, file, allow_unicode=True, sort_keys=False)
        os.replace(temporary, self.path)
        self.cfg = load_config(self.path)

    def public(self) -> dict:
        cfg = self.cfg
        return {
            "access": {"user_login": self.user_login},
            "media": asdict(self.media),
            "owui": {"base_url": cfg.owui.base_url},
            "display": {
                "reasoning": {
                    "enable": cfg.display.reasoning.enable,
                    "detailed": cfg.display.reasoning.detailed,
                },
                "tool_status": {
                    "enable": cfg.display.tool_status.enable,
                    "detailed": cfg.display.tool_status.detailed,
                },
                "typing": cfg.display.typing, "citations": cfg.display.citations,
            },
            "capabilities": {
                key: getattr(cfg.capabilities, key)
                for key in ("memory", "tools", "web_search", "allow_terminal", "image_generation")
            },
            "reply": {"max_length": cfg.reply.max_length, "segment_interval": cfg.reply.segment_interval},
        }

    @property
    def user_login(self) -> bool:
        raw = self.read()
        return bool(raw.get("access", {}).get("user_login", False))

    @property
    def media(self) -> MediaConfig:
        return MediaConfig.load(self.read().get("media", {}))

    def merge(self, incoming: dict) -> dict:
        raw = self.read()
        if "owui" in incoming and "base_url" in incoming["owui"]:
            raw.setdefault("owui", {})["base_url"] = validate_url(incoming["owui"]["base_url"])
        if "user_login" in incoming.get("access", {}):
            value = incoming["access"]["user_login"]
            if type(value) is not bool:
                raise ValueError("用户登录开关必须为布尔值")
            raw.setdefault("access", {})["user_login"] = value
        for path in BOOL_PATHS:
            keys = path.split(".")
            source = incoming
            for key in keys[:-1]:
                source = source.get(key, {}) if isinstance(source, dict) else {}
            if not isinstance(source, dict) or keys[-1] not in source:
                continue
            value = source[keys[-1]]
            if type(value) is not bool:
                raise ValueError("开关设置必须为布尔值")
            target = raw
            for key in keys[:-1]:
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                target = target[key]
            target[keys[-1]] = value
        reply = incoming.get("reply", {})
        if "media" in incoming:
            raw["media"] = asdict(MediaConfig.load({
                **raw.get("media", {}), **incoming["media"],
            }))
        if "max_length" in reply:
            value = reply["max_length"]
            if type(value) is not int or not 128 <= value <= 8192:
                raise ValueError("单条消息长度需为 128–8192")
            raw["reply"]["max_length"] = value
        if "segment_interval" in reply:
            value = reply["segment_interval"]
            if type(value) not in (int, float) or not 0 <= value <= 5:
                raise ValueError("消息间隔需为 0–5 秒")
            raw["reply"]["segment_interval"] = value
        return raw
