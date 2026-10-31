"""Bounded image IO; bearer credentials never cross Open WebUI/CDN boundaries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from open_webui_weixin.weixin_protocol import SessionExpiredError
from PIL import Image, UnidentifiedImageError

from .config import MediaConfig


class MediaError(ValueError):
    """A safe, user-facing error; never include signed URLs or encryption keys."""


@dataclass(frozen=True)
class ImageData:
    data: bytes
    mime: str
    extension: str

    def inline(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


def normalize_image(data: bytes, limit: int) -> ImageData:
    if len(data) > limit:
        raise MediaError("图片超过单张大小上限")
    try:
        with Image.open(io.BytesIO(data)) as image:
            kind = image.format
            if kind not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise MediaError("仅支持 JPEG、PNG、WebP 和 GIF 图片")
            if image.width * image.height > 40_000_000:
                raise MediaError("图片超过 4000 万像素上限")
            image.seek(0)
            image.load()
            if kind == "GIF" or getattr(image, "is_animated", False):
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")
                data, kind = output.getvalue(), "PNG"
                if len(data) > limit:
                    raise MediaError("图片首帧超过单张大小上限")
            mime, extension = {
                "JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"),
                "WEBP": ("image/webp", "webp"),
            }[kind]
            return ImageData(data, mime, extension)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("图片内容损坏或格式不受支持") from None


def parse_aes_key(value: str, *, hexadecimal: bool = False) -> bytes:
    try:
        decoded = bytes.fromhex(value) if hexadecimal else base64.b64decode(value, validate=True)
        if len(decoded) == 32:
            decoded = bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) != 16:
            raise ValueError()
        return decoded
    except (ValueError, binascii.Error, UnicodeError, TypeError):
        raise MediaError("微信图片密钥格式无效") from None


def encrypt_image(data: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_image(data: bytes, key: bytes) -> bytes:
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise MediaError("微信图片解密失败，请重新发送图片") from None


async def read_limited(client: httpx.AsyncClient, url: str, limit: int, *, headers=None) -> bytes:
    try:
        async with client.stream("GET", url, headers=headers, follow_redirects=False, timeout=25) as response:
            if not 200 <= response.status_code < 300:
                raise MediaError("图片下载失败，请重试")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > limit:
                raise MediaError("图片超过大小上限")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise MediaError("图片超过大小上限")
                chunks.append(chunk)
            return b"".join(chunks)
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("图片下载失败，请重试") from None


def cdn_url(value: str, base_url: str) -> str:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        trusted = (urlsplit(base_url).hostname or "").lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise MediaError("微信图片地址无效")
        if (
            parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment
            or not host or not (host == trusted or host.endswith(".cdn.weixin.qq.com"))
        ):
            raise MediaError("微信图片地址不在受信 CDN 中")
        return value
    except ValueError:
        raise MediaError("微信图片地址无效") from None


class WeixinImages:
    def __init__(self, settings: MediaConfig, *, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)
        self.slots = asyncio.Semaphore(4)

    async def download(self, ilink, item: dict) -> ImageData:
        image = item.get("image_item") or {}
        media = image.get("media") or {}
        source = media.get("full_url")
        if not source:
            query = media.get("encrypt_query_param")
            if not isinstance(query, str) or not query:
                raise MediaError("微信图片缺少下载信息，请重新发送")
            source = ilink.build_download_url(query)
        source = cdn_url(source, ilink.cdn_base_url)
        async with self.slots:
            encrypted = await read_limited(
                self.client, source, ((self.settings.max_image_bytes // 16) + 1) * 16,
            )
            if image.get("aeskey"):
                data = decrypt_image(encrypted, parse_aes_key(image["aeskey"], hexadecimal=True))
            elif media.get("aes_key"):
                data = decrypt_image(encrypted, parse_aes_key(media["aes_key"]))
            else:
                data = encrypted
            return await asyncio.to_thread(normalize_image, data, self.settings.max_image_bytes)

    async def upload(self, ilink, token: str, uid: str, image: ImageData) -> dict:
        key = secrets.token_bytes(16)
        encrypted = encrypt_image(image.data, key)
        filekey = secrets.token_hex(16)
        try:
            response = await ilink._post_json(
                "ilink/bot/getuploadurl", {
                    "filekey": filekey, "media_type": 1, "to_user_id": uid,
                    "rawsize": len(image.data),
                    "rawfilemd5": hashlib.md5(image.data, usedforsecurity=False).hexdigest(),
                    "filesize": len(encrypted), "no_need_thumb": True,
                    "aeskey": key.hex(), "base_info": ilink._base_info(),
                }, token, label="getUploadUrl",
            )
            ilink._check_ok(response, "getUploadUrl")
            address = response.get("upload_full_url")
            if not address and response.get("upload_param"):
                address = ilink.build_upload_url(response["upload_param"], filekey)
            address = cdn_url(address or "", ilink.cdn_base_url)
            async with self.slots:
                uploaded = await self.client.post(
                    address, content=encrypted, headers={"Content-Type": "application/octet-stream"},
                    timeout=25, follow_redirects=False,
                )
            if not 200 <= uploaded.status_code < 300 or not uploaded.headers.get("x-encrypted-param"):
                raise MediaError("微信图片上传失败")
            return {
                "type": 2, "image_item": {
                    "media": {
                        "encrypt_query_param": uploaded.headers["x-encrypted-param"],
                        "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
                        "encrypt_type": 1,
                    },
                    "mid_size": len(encrypted),
                },
            }
        except SessionExpiredError:
            raise
        except Exception:
            raise MediaError("微信图片上传失败，请在 OpenWebUI 查看图片") from None

    async def close(self):
        await self.client.aclose()


class OwuiImages:
    def __init__(self, owui, settings: MediaConfig):
        self.owui = owui
        self.settings = settings

    def owned_url(self, source: str) -> str | None:
        if not isinstance(source, str):
            return None
        base = urlsplit(self.owui.base_url)
        try:
            if len(source) in (32, 36) and all(c in "0123456789abcdefABCDEF-" for c in source):
                source = f"/api/v1/files/{uuid.UUID(source)}/content"
            if source.startswith(("/api/v1/files/", "/api/v1/images/", "/cache/image/")):
                address = self.owui.base_url + source
            else:
                address = urljoin(self.owui.base_url + "/", source)
            parsed = urlsplit(address)
            if (
                (parsed.scheme, parsed.hostname, parsed.port) != (base.scheme, base.hostname, base.port)
                or parsed.username or parsed.password or parsed.fragment
            ):
                return None
            prefix = base.path.rstrip("/")
            path = parsed.path.removeprefix(prefix)
            if prefix and not parsed.path.startswith(prefix + "/"):
                return None
            if not path.startswith(("/api/v1/files/", "/api/v1/images/", "/cache/image/")):
                return None
            if ".." in path.split("/") or "%" in path:
                return None
            return address
        except ValueError:
            return None

    def supported(self, source: str) -> bool:
        return isinstance(source, str) and (
            source.startswith("data:image/") or self.owned_url(source) is not None
        )

    async def fetch(self, token: str, source: str) -> ImageData:
        if source.startswith("data:image/"):
            if len(source) > (self.settings.max_image_bytes * 4 // 3) + 128:
                raise MediaError("回复图片超过单张大小上限")
            try:
                header, encoded = source.split(",", 1)
                if not header.endswith(";base64"):
                    raise ValueError()
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise MediaError("回复图片内容无效") from None
        else:
            address = self.owned_url(source)
            if not address:
                raise MediaError("仅转发 OpenWebUI 保存的图片")
            data = await read_limited(
                self.owui._client, address, self.settings.max_image_bytes,
                headers={"Authorization": f"Bearer {token}"},
            )
        return await asyncio.to_thread(normalize_image, data, self.settings.max_image_bytes)

    async def upload(self, token: str, image: ImageData, name: str) -> dict:
        try:
            response = await self.owui._client.post(
                f"{self.owui.base_url}/api/v1/files/", params={"process": "false"},
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (f"{name}.{image.extension}", image.data, image.mime)},
                follow_redirects=False, timeout=30,
            )
            if not 200 <= response.status_code < 300:
                raise MediaError("图片上传 OpenWebUI 失败，请重新发送问题重试")
            file_id = response.json().get("id")
            if (
                not isinstance(file_id, str) or not file_id
                or any(c not in "0123456789abcdef-" for c in file_id)
            ):
                raise MediaError("OpenWebUI 返回的图片附件无效")
            return {
                "id": file_id, "type": "image", "name": f"{name}.{image.extension}",
                "url": f"/api/v1/files/{file_id}/content",
                "content_type": image.mime, "size": len(image.data),
            }
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, MediaError):
                raise
            raise MediaError("图片上传 OpenWebUI 失败，请重新发送问题重试") from None
