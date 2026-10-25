"""Local administrator authentication; passwords and sessions never enter API responses."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path


class AdminAuth:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[str, float] = {}
        self.attempts: dict[str, list[float]] = {}
        self.lock = asyncio.Lock()
        self.record = json.loads(path.read_text()) if path.exists() else None

    @property
    def initialized(self) -> bool:
        return self.record is not None

    @staticmethod
    def validate_password(password: str) -> None:
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            raise ValueError("管理密码需为 8–256 个字符")

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1
        ).hex()

    def _persist(self, record: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(record, file)
        os.replace(temporary, self.path)
        self.record = record

    async def setup(self, password: str) -> str:
        self.validate_password(password)
        async with self.lock:
            if self.initialized:
                raise ValueError("管理密码已经设置")
            salt = secrets.token_hex(24)
            digest = await asyncio.to_thread(self._hash, password, salt)
            self._persist({"salt": salt, "hash": digest})
        return self.issue_session()

    def issue_session(self) -> str:
        now = time.time()
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + 12 * 3600
        return token

    def valid(self, token: str) -> bool:
        return bool(token) and self.sessions.get(token, 0) > time.time()

    async def login(self, password: str, peer: str) -> str:
        now = time.time()
        recent = [stamp for stamp in self.attempts.get(peer, []) if stamp > now - 60]
        self.attempts[peer] = recent
        if len(recent) >= 5:
            raise ValueError("尝试过于频繁，请一分钟后重试")
        recent.append(now)
        if not self.initialized or not isinstance(password, str) or len(password) > 256:
            raise ValueError("管理密码不正确")
        digest = await asyncio.to_thread(self._hash, password, self.record["salt"])
        if not hmac.compare_digest(digest, self.record["hash"]):
            raise ValueError("管理密码不正确")
        self.attempts.pop(peer, None)
        return self.issue_session()

    async def change_password(self, old: str, new: str) -> str:
        self.validate_password(new)
        async with self.lock:
            if not self.record or not isinstance(old, str) or len(old) > 256:
                raise ValueError("当前管理密码不正确")
            digest = await asyncio.to_thread(self._hash, old, self.record["salt"])
            if not hmac.compare_digest(digest, self.record["hash"]):
                raise ValueError("当前管理密码不正确")
            salt = secrets.token_hex(24)
            digest = await asyncio.to_thread(self._hash, new, salt)
            self._persist({"salt": salt, "hash": digest})
            self.sessions.clear()
            return self.issue_session()
