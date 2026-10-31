"""Private, expiring image batches. Only unsubmitted batches survive process restarts."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import MediaConfig
from .image_transport import ImageData, MediaError, OwuiImages, WeixinImages

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingTurn:
    text: str
    scope: str
    batch_id: str | None = None


class ImageStore:
    def __init__(self, directory: Path, settings: MediaConfig):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.settings = settings
        path = directory / "inbox.db"
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS batches(
                id TEXT PRIMARY KEY, uid TEXT NOT NULL, scope TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS batch_owner ON batches(uid, status);
            CREATE TABLE IF NOT EXISTS images(
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                size INTEGER NOT NULL DEFAULT 0, mime TEXT, extension TEXT, uploaded TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS seen(
                id TEXT PRIMARY KEY, created REAL NOT NULL
            );
        """)
        for row in self.db.execute("SELECT id FROM batches WHERE status='submitting'").fetchall():
            self.drop(row["id"])
        self.db.execute("UPDATE batches SET status='pending' WHERE status='held'")
        self.db.execute("UPDATE images SET status='pending' WHERE status='fetching'")
        referenced = {row["id"] for row in self.db.execute("SELECT id FROM images")}
        for candidate in directory.glob("*.image"):
            if candidate.stem not in referenced:
                candidate.unlink()
        for candidate in directory.glob("*.tmp"):
            candidate.unlink()

    def path(self, image_id: str) -> Path:
        if len(image_id) != 32 or any(c not in "0123456789abcdef" for c in image_id):
            raise MediaError("图片引用无效")
        return self.directory / f"{image_id}.image"

    def seen(self, account: str, message: dict) -> bool:
        message_id = message.get("message_id") or message.get("msg_id") or message.get("client_id")
        if message_id is None:
            return False
        key = hashlib.sha256(f"{account}:{message_id}".encode()).hexdigest()
        now = time.time()
        self.db.execute("DELETE FROM seen WHERE created < ?", (now - 86400,))
        cursor = self.db.execute("INSERT OR IGNORE INTO seen VALUES(?, ?)", (key, now))
        return cursor.rowcount == 0

    def add(self, uid: str, scope: str, items: list[dict]) -> tuple[str, bool, list[str]]:
        now = time.time()
        batch = self.db.execute(
            "SELECT * FROM batches WHERE uid=? AND scope=? AND status='pending' AND expires>? "
            "ORDER BY created DESC LIMIT 1", (uid, scope, now),
        ).fetchone()
        existing = self.images(batch["id"]) if batch else []
        if len(existing) + len(items) > self.settings.max_images_per_turn:
            raise MediaError(f"每批最多 {self.settings.max_images_per_turn} 张图片，请先发送问题")
        # Bound retained batches while a slow model is generating.
        count = self.db.execute("SELECT count(*) FROM batches WHERE uid=?", (uid,)).fetchone()[0]
        if not batch and count >= 6:
            raise MediaError("待处理图片批次过多，请先等待已有问题完成")
        first = batch is None
        batch_id = batch["id"] if batch else uuid.uuid4().hex
        if first:
            self.db.execute(
                "INSERT INTO batches VALUES(?,?,?,?,?,'pending')",
                (batch_id, uid, scope, now, now + self.settings.pending_ttl_seconds),
            )
        ids = []
        for offset, source in enumerate(items, len(existing)):
            image_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO images(id,batch_id,position,source) VALUES(?,?,?,?)",
                (image_id, batch_id, offset, json.dumps(source)),
            )
            ids.append(image_id)
        return batch_id, first, ids

    def images(self, batch_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM images WHERE batch_id=? ORDER BY position", (batch_id,),
        ).fetchall()

    def batch(self, batch_id: str):
        return self.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()

    def claim(self, uid: str, scope: str) -> str | None:
        batch = self.db.execute(
            "SELECT id FROM batches WHERE uid=? AND scope=? AND status='pending' AND expires>? "
            "ORDER BY created LIMIT 1", (uid, scope, time.time()),
        ).fetchone()
        if not batch:
            return None
        self.db.execute("UPDATE batches SET status='held' WHERE id=?", (batch["id"],))
        return batch["id"]

    def retry(self, batch_id: str) -> None:
        self.db.execute("UPDATE batches SET status='pending' WHERE id=?", (batch_id,))
        self.db.execute(
            "UPDATE images SET status='pending' WHERE batch_id=? AND status='error'", (batch_id,),
        )

    def drop(self, batch_id: str) -> None:
        for image in self.images(batch_id):
            self.path(image["id"]).unlink(missing_ok=True)
        self.db.execute("DELETE FROM batches WHERE id=?", (batch_id,))

    def drop_user(self, uid: str) -> None:
        for row in self.db.execute("SELECT id FROM batches WHERE uid=?", (uid,)).fetchall():
            self.drop(row["id"])

    def expire(self) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT * FROM batches WHERE status='pending' AND expires<=?", (time.time(),),
        ).fetchall()
        for row in rows:
            self.drop(row["id"])
        return list({(row["uid"], row["scope"]) for row in rows})

    def save(self, image_id: str, image: ImageData) -> None:
        row = self.db.execute("SELECT batch_id FROM images WHERE id=?", (image_id,)).fetchone()
        if not row:
            return
        siblings = self.images(row["batch_id"])
        total = sum(item["size"] for item in siblings if item["id"] != image_id) + len(image.data)
        if total > self.settings.max_batch_bytes:
            raise MediaError("本批图片总大小超过上限，请减少图片后重新发送")
        path = self.path(image_id)
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(image.data)
        os.replace(temporary, path)
        self.db.execute(
            "UPDATE images SET status='ready',size=?,mime=?,extension=?,error=NULL WHERE id=?",
            (len(image.data), image.mime, image.extension, image_id),
        )

    def read(self, row) -> ImageData:
        try:
            data = self.path(row["id"]).read_bytes()
        except OSError:
            raise MediaError("暂存图片已丢失，请重新发送图片") from None
        if len(data) > self.settings.max_image_bytes:
            raise MediaError("暂存图片超过当前大小上限")
        return ImageData(data, row["mime"], row["extension"])

    def close(self):
        self.db.close()


class ImageInbox:
    def __init__(self, directory: Path, settings: MediaConfig):
        self.store = ImageStore(directory, settings)
        self.settings = settings
        self.weixin = WeixinImages(settings)
        self.tasks: dict[str, asyncio.Task] = {}
        self.sweeper: asyncio.Task | None = None
        self.adapter = None

    def attach(self, adapter):
        self.adapter = adapter
        self.start_cleanup()
        if not self.settings.enabled:
            for row in self.store.db.execute("SELECT DISTINCT uid FROM batches").fetchall():
                self.clear(row["uid"])
            return
        for batch in self.store.db.execute("SELECT * FROM batches").fetchall():
            if not self.valid(batch["uid"], batch["scope"]):
                self.store.drop(batch["id"])
                continue
            for image in self.store.images(batch["id"]):
                self.ensure_download(image["id"])

    def start_cleanup(self):
        if self.sweeper is None or self.sweeper.done():
            self.sweeper = asyncio.create_task(self._sweep())

    def valid(self, uid: str, scope: str) -> bool:
        return bool(self.adapter and self.adapter.scope(uid) == scope)

    def ensure_download(self, image_id: str):
        task = self.tasks.get(image_id)
        if task and not task.done():
            return task
        task = asyncio.create_task(self._download(image_id))
        self.tasks[image_id] = task
        task.add_done_callback(
            lambda done: self.tasks.pop(image_id, None) if self.tasks.get(image_id) is done else None
        )
        return task

    async def _download(self, image_id: str):
        row = self.store.db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        if not row:
            return
        if row["status"] == "ready" and self.store.path(image_id).exists():
            return
        batch = self.store.batch(row["batch_id"])
        if not batch or not self.valid(batch["uid"], batch["scope"]):
            return
        scope = json.loads(batch["scope"])
        handle = self.adapter._accounts.get(scope["account"])
        if not handle:
            return
        self.store.db.execute("UPDATE images SET status='fetching' WHERE id=?", (image_id,))
        try:
            image = await self.weixin.download(handle.client, json.loads(row["source"]))
            if self.store.batch(batch["id"]) and self.valid(batch["uid"], batch["scope"]):
                self.store.save(image_id, image)
        except asyncio.CancelledError:
            self.store.db.execute("UPDATE images SET status='pending' WHERE id=?", (image_id,))
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, MediaError) else "图片下载或解密失败，请重新发送问题重试"
            log.warning(
                "微信图片准备未完成：%s", error if isinstance(exc, MediaError) else type(exc).__name__,
            )
            self.store.db.execute("UPDATE images SET status='error',error=? WHERE id=?", (error, image_id))

    async def prepare(self, batch_id: str, uid: str, scope: str, token: str, temporary: bool) -> list[dict]:
        batch = self.store.batch(batch_id)
        if not batch or batch["scope"] != scope or not self.valid(uid, scope):
            raise MediaError("图片所属会话已变化，请重新发送图片")
        for row in self.store.images(batch_id):
            await self.ensure_download(row["id"])
        rows = self.store.images(batch_id)
        if not rows or any(row["status"] != "ready" for row in rows):
            error = next((row["error"] for row in rows if row["error"]), None)
            raise MediaError(error or "图片下载或解密失败，请重新发送问题重试，或重新发送图片")
        if (
            len(rows) > self.settings.max_images_per_turn
            or sum(r["size"] for r in rows) > self.settings.max_batch_bytes
        ):
            raise MediaError("本批图片超过当前数量或大小上限，请重新发送")
        files = []
        access = OwuiImages(self.adapter.owui, self.settings)
        for row in rows:
            if not self.valid(uid, scope):
                raise MediaError("图片所属账号或会话已变化")
            image = self.store.read(row)
            if temporary:
                file = {"type": "image", "url": image.inline(), "name": f"image.{image.extension}"}
            elif row["uploaded"]:
                file = json.loads(row["uploaded"])
            else:
                file = await access.upload(token, image, row["id"])
                if not self.valid(uid, scope):
                    raise MediaError("图片所属账号或会话已变化")
                self.store.db.execute(
                    "UPDATE images SET uploaded=? WHERE id=?", (json.dumps(file), row["id"]),
                )
            files.append(file)
        return files

    async def _sweep(self):
        while True:
            for uid, scope in self.store.expire():
                if self.valid(uid, scope):
                    await self.adapter.send_text(uid, "上一批待发送图片已过期，请重新发送图片后再提问。")
            await asyncio.sleep(1)

    def clear(self, uid: str):
        ids = [
            row["id"] for batch in self.store.db.execute("SELECT id FROM batches WHERE uid=?", (uid,))
            for row in self.store.images(batch["id"])
        ]
        for image_id in ids:
            task = self.tasks.get(image_id)
            if task:
                task.cancel()
        self.store.drop_user(uid)

    async def detach(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.adapter = None

    async def close(self):
        if self.sweeper:
            self.sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sweeper
            self.sweeper = None
        await self.detach()
        await self.weixin.close()
        self.store.close()
