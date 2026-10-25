"""Server-side sessions and persistent credential-attempt budgets."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from open_webui_weixin.owui import SessionInfo


class RateLimited(ValueError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(f"尝试过于频繁，请 {self.retry_after} 秒后重试")


class LoginLimiter:
    """Reserve all budgets before awaiting credential checks, including successful attempts."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        path.chmod(0o600)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS attempts (bucket TEXT NOT NULL, at REAL NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS attempts_bucket ON attempts(bucket, at)")

    def check(self, peer: str, identity: str) -> None:
        now = time.time()
        budgets = (
            ("global", 60, 60),
            (f"peer:{peer}", 20, 900),
            (f"identity:{identity}", 5, 900),
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DELETE FROM attempts WHERE at <= ?", (now - 900,))
            retry = 0
            keys = []
            for raw, limit, window in budgets:
                key = hashlib.sha256(raw.encode()).hexdigest()
                keys.append(key)
                rows = self.db.execute(
                    "SELECT at FROM attempts WHERE bucket = ? AND at > ? ORDER BY at",
                    (key, now - window),
                ).fetchall()
                if len(rows) >= limit:
                    retry = max(retry, int(rows[-limit][0] + window - now) + 1)
            if retry:
                raise RateLimited(retry)
            self.db.executemany(
                "INSERT INTO attempts(bucket, at) VALUES(?, ?)", [(key, now) for key in keys]
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()


@dataclass
class Principal:
    role: str
    expires_at: float
    owui: SessionInfo | None = field(default=None, repr=False)
    base_url: str = ""
    checked_at: float = 0
    check_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def active(self) -> bool:
        return self.expires_at > time.time()

    def public(self) -> dict:
        return {
            "role": self.role,
            "user": {"name": self.owui.name, "email": self.owui.email} if self.owui else None,
        }


class AdminAuth:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[str, Principal] = {}
        self.limiter = LoginLimiter(path.with_name("login-attempts.db"))
        self.credential_slots = asyncio.Semaphore(4)
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

    def issue_session(self, owui: SessionInfo | None = None, base_url: str = "") -> str:
        now = time.time()
        self.sessions = {key: value for key, value in self.sessions.items() if value.active}
        if len(self.sessions) >= 2048:
            raise ValueError("登录会话过多，请稍后重试")
        token = secrets.token_urlsafe(32)
        expiry = min(now + 12 * 3600, owui.expires_at) if owui and owui.expires_at else now + 12 * 3600
        self.sessions[token] = Principal(
            "user" if owui else "admin", expiry, owui, base_url, time.monotonic()
        )
        return token

    def principal(self, token: str) -> Principal | None:
        value = self.sessions.get(token)
        return value if value and value.active else None

    def valid(self, token: str) -> bool:
        return self.principal(token) is not None

    def revoke(self, token: str) -> None:
        value = self.sessions.pop(token, None)
        if value:
            value.expires_at = 0

    @asynccontextmanager
    async def credential_check(self, peer: str, identity: str):
        self.limiter.check(peer, identity)
        if self.credential_slots.locked():
            raise RateLimited(2)
        async with self.credential_slots:
            yield

    async def login(self, password: str, peer: str) -> str:
        async with self.credential_check(peer, "admin"):
            if not self.initialized or not isinstance(password, str) or len(password) > 256:
                raise ValueError("管理密码不正确")
            digest = await asyncio.to_thread(self._hash, password, self.record["salt"])
            if not hmac.compare_digest(digest, self.record["hash"]):
                raise ValueError("管理密码不正确")
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
            for value in self.sessions.values():
                if value.role == "admin":
                    value.expires_at = 0
            return self.issue_session()
