from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from open_webui_weixin.capabilities import RequestCaps
from open_webui_weixin.chat import ChatRunner, TurnResult, _serialize_temporary_history
from open_webui_weixin.owui import OwuiClient
from open_webui_weixin.render import TurnRenderer
from open_webui_weixin.state import StateStore
from open_webui_weixin.weixin_protocol import IlinkClient
from PIL import Image

from oui_wechat.adapter import WebAdapter
from oui_wechat.config import ConfigStore, MediaConfig
from oui_wechat.image_chat import ImageChatRunner, ImageTextFilter, image_sources, markdown_images
from oui_wechat.image_inbox import ImageInbox, ImageStore
from oui_wechat.image_transport import (
    ImageData,
    MediaError,
    OwuiImages,
    WeixinImages,
    cdn_url,
    decrypt_image,
    encrypt_image,
    normalize_image,
    parse_aes_key,
)
from oui_wechat.logs import redact


def picture(color="red", fmt="PNG") -> ImageData:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format=fmt)
    return normalize_image(output.getvalue(), 1024 * 1024)


def ilink(transport=None):
    return IlinkClient(
        base_url="https://api.wechat.example", cdn_base_url="https://cdn.weixin.qq.com/c2c",
        channel_version="2.4.9", bot_agent="fixture", transport=transport,
    )


class ImageCodecTests(unittest.TestCase):
    def test_aes_round_trip_and_all_supported_key_encodings(self):
        key = bytes(range(16))
        for length in (0, 1, 15, 16, 17, 1024):
            raw = b"x" * length
            encrypted = encrypt_image(raw, key)
            self.assertEqual(len(encrypted), (length // 16 + 1) * 16)
            self.assertEqual(decrypt_image(encrypted, key), raw)
        self.assertEqual(parse_aes_key(base64.b64encode(key).decode()), key)
        self.assertEqual(parse_aes_key(base64.b64encode(key.hex().encode()).decode()), key)
        self.assertEqual(parse_aes_key(key.hex(), hexadecimal=True), key)
        for invalid in ("secret-invalid-key", "AAAA", "%%%", ""):
            with self.assertRaises(MediaError) as caught:
                parse_aes_key(invalid)
            self.assertNotIn("secret-invalid-key", str(caught.exception))
        with self.assertRaises(MediaError):
            decrypt_image(b"broken", key)

    def test_image_validation_formats_size_and_gif_first_frame(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            image = picture(fmt=fmt)
            self.assertTrue(image.mime.startswith("image/"))
        image = picture(fmt="GIF")
        self.assertEqual(image.mime, "image/png")
        with self.assertRaises(MediaError):
            normalize_image(picture().data, 4)
        for data in (b"not an image", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"):
            with self.assertRaises(MediaError):
                normalize_image(data, 1024)

    def test_pixel_limit_is_checked_before_decoding(self):
        fake = SimpleNamespace(format="PNG", width=10000, height=10000)
        with patch("oui_wechat.image_transport.Image.open") as opened:
            opened.return_value.__enter__.return_value = fake
            with self.assertRaisesRegex(MediaError, "4000"):
                normalize_image(b"x", 1024)

    def test_cdn_urls_are_https_allowlisted_and_do_not_accept_credentials(self):
        base = "https://cdn.weixin.qq.com/c2c"
        self.assertEqual(cdn_url(base + "/download?key=fixture", base), base + "/download?key=fixture")
        self.assertTrue(cdn_url("https://novac2c.cdn.weixin.qq.com/c2c/download", base))
        for source in (
            "http://cdn.weixin.qq.com/x", "https://evil.example/x",
            "https://cdn.weixin.qq.com.evil.example/x", "https://127.0.0.1/x",
            "https://cdn.weixin.qq.com:8443/x", "https://u:p@cdn.weixin.qq.com/x",
            "file:///etc/passwd", "https://cdn.weixin.qq.com/x#fragment",
        ):
            with self.assertRaises(MediaError, msg=source):
                cdn_url(source, base)

    def test_secrets_and_inline_images_are_redacted(self):
        value = redact(
            'aes_key="private-key" aeskey=private-hex encrypt_query_param=private-query '
            'https://cdn.example/download?secret=private-param data:image/png;base64,AAAA'
        )
        for secret in ("private-key", "private-hex", "private-query", "private-param", "AAAA"):
            self.assertNotIn(secret, value)


class ImageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_inbound_image_never_sends_bearer_to_cdn(self):
        image, key = picture(), bytes(range(16))
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=encrypt_image(image.data, key))

        api = ilink()
        media = WeixinImages(MediaConfig(), transport=httpx.MockTransport(handler))
        try:
            for fields in (
                {"aeskey": key.hex(), "media": {"encrypt_query_param": "signed-fixture"}},
                {"media": {"encrypt_query_param": "signed-fixture",
                           "aes_key": base64.b64encode(key).decode()}},
                {"media": {"full_url": "https://novac2c.cdn.weixin.qq.com/file",
                           "aes_key": base64.b64encode(key.hex().encode()).decode()}},
            ):
                result = await media.download(api, {"image_item": fields})
                self.assertEqual(result.data, image.data)
            self.assertTrue(all("Authorization" not in request.headers for request in requests))
        finally:
            await media.close()
            await api.close()

    async def test_download_rejects_redirects_and_oversized_bodies(self):
        api = ilink()
        for response in (
            httpx.Response(302, headers={"Location": "https://evil.example/image"}),
            httpx.Response(200, headers={"Content-Length": "999999999"}, content=b"x"),
            httpx.Response(200, content=b"x" * 2048),
        ):
            media = WeixinImages(
                MediaConfig(max_image_bytes=1024),
                transport=httpx.MockTransport(lambda req, result=response: result),
            )
            try:
                with self.assertRaises(MediaError):
                    await media.download(api, {"image_item": {
                        "media": {"full_url": "https://novac2c.cdn.weixin.qq.com/file"},
                    }})
            finally:
                await media.close()
        await api.close()

    async def test_upload_encrypts_bytes_and_builds_native_wechat_image(self):
        image = picture()
        captured = {}

        def api_handler(request):
            self.assertEqual(request.url.path, "/ilink/bot/getuploadurl")
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-bot-token")
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={
                "upload_full_url": "https://novac2c.cdn.weixin.qq.com/upload?fixture=1",
            })

        def cdn_handler(request):
            self.assertNotIn("Authorization", request.headers)
            key = bytes.fromhex(captured["aeskey"])
            self.assertEqual(decrypt_image(request.content, key), image.data)
            self.assertEqual(len(request.content), captured["filesize"])
            return httpx.Response(200, headers={"x-encrypted-param": "fixture-download-param"})

        api = ilink(httpx.MockTransport(api_handler))
        media = WeixinImages(MediaConfig(), transport=httpx.MockTransport(cdn_handler))
        try:
            item = await media.upload(api, "fixture-bot-token", "fixture-user", image)
            self.assertEqual(captured["media_type"], 1)
            self.assertEqual(captured["rawsize"], len(image.data))
            self.assertEqual(item["type"], 2)
            self.assertEqual(item["image_item"]["media"]["encrypt_query_param"], "fixture-download-param")
            self.assertEqual(
                parse_aes_key(item["image_item"]["media"]["aes_key"]),
                bytes.fromhex(captured["aeskey"]),
            )
        finally:
            await api.close()
            await media.close()

    async def test_openwebui_upload_preserves_ownership_and_disables_document_processing(self):
        file_id = str(uuid.uuid4())
        image = picture()
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-owui-token")
            if request.method == "POST":
                self.assertEqual(request.url.params["process"], "false")
                self.assertIn(image.data, request.content)
                return httpx.Response(200, json={"id": file_id})
            return httpx.Response(200, content=image.data)

        client = OwuiClient("http://127.0.0.1:8901", transport=httpx.MockTransport(handler))
        access = OwuiImages(client, MediaConfig())
        try:
            uploaded = await access.upload("fixture-owui-token", image, "fixture")
            self.assertEqual(uploaded["id"], file_id)
            self.assertEqual(uploaded["type"], "image")
            self.assertEqual(uploaded["url"], f"/api/v1/files/{file_id}/content")
            downloaded = await access.fetch("fixture-owui-token", uploaded["url"])
            self.assertEqual(downloaded.data, image.data)
            before = len(requests)
            for source in ("https://external.example/image.png", "//evil.example/x",
                           "http://127.0.0.1:8901/api/v1/files/../auths",
                           "http://u:p@127.0.0.1:8901/api/v1/files/id/content"):
                self.assertFalse(access.supported(source))
            self.assertEqual(len(requests), before)
            self.assertEqual((await access.fetch("fixture-owui-token", image.inline())).data, image.data)
            self.assertEqual(
                access.owned_url(file_id), f"http://127.0.0.1:8901/api/v1/files/{file_id}/content",
            )
        finally:
            await client.close()


class ImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "media"
        self.store = ImageStore(self.path, MediaConfig())

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_fixed_deadline_and_claim_boundary(self):
        with patch("oui_wechat.image_inbox.time.time", return_value=1000):
            batch, first, _ = self.store.add("alice", "scope", [{"one": 1}])
        with patch("oui_wechat.image_inbox.time.time", return_value=1100):
            second, first_again, _ = self.store.add("alice", "scope", [{"two": 2}])
            self.assertEqual(second, batch)
            self.assertTrue(first)
            self.assertFalse(first_again)
            self.assertEqual(self.store.batch(batch)["expires"], 1120)
            self.assertEqual(self.store.claim("alice", "scope"), batch)
            later, _, _ = self.store.add("alice", "scope", [{"later": 3}])
            self.assertNotEqual(later, batch)
        with patch("oui_wechat.image_inbox.time.time", return_value=1221):
            self.assertEqual(self.store.expire(), [("alice", "scope")])
            self.assertIsNotNone(self.store.batch(batch))
            self.assertIsNone(self.store.batch(later))

    def test_restart_restores_unsubmitted_but_never_replays_submission(self):
        keep, _, ids = self.store.add("alice", "scope", [{}])
        self.store.save(ids[0], picture())
        claimed = self.store.claim("alice", "scope")
        uncertain, _, _ = self.store.add("bob", "other", [{}])
        self.store.db.execute("UPDATE batches SET status='submitting' WHERE id=?", (uncertain,))
        self.store.close()
        self.store = ImageStore(self.path, MediaConfig())
        self.assertEqual(claimed, keep)
        self.assertEqual(self.store.batch(keep)["status"], "pending")
        self.assertEqual(self.store.images(keep)[0]["status"], "ready")
        self.assertIsNone(self.store.batch(uncertain))
        self.assertEqual(self.store.path(ids[0]).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.path / "inbox.db").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)

    def test_count_batch_size_and_scope_isolation(self):
        batch, _, ids = self.store.add("alice", "scope", [{}] * 5)
        with self.assertRaises(MediaError):
            self.store.add("alice", "scope", [{}])
        self.assertIsNone(self.store.claim("bob", "scope"))
        self.assertIsNone(self.store.claim("alice", "other-scope"))
        self.store.settings = MediaConfig(max_batch_bytes=1)
        with self.assertRaises(MediaError):
            self.store.save(ids[0], picture())
        self.store.drop(batch)
        self.assertEqual(self.store.images(batch), [])

    def test_duplicate_inbound_ids_are_account_scoped(self):
        message = {"message_id": 123456789012345678901234567890}
        self.assertFalse(self.store.seen("bot-a", message))
        self.assertTrue(self.store.seen("bot-a", message))
        self.assertFalse(self.store.seen("bot-b", message))


class ImageOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_markdown_filter_handles_stream_boundaries_and_preserves_external_images(self):
        client = OwuiClient("https://owui.example")
        access = OwuiImages(client, MediaConfig())
        try:
            text = ImageTextFilter(access)
            self.assertEqual(text.feed("前文 !"), "前文 ")
            self.assertEqual(text.feed("[图](data:image/png;base64,"), "")
            self.assertEqual(text.feed("AAAA"), "")
            self.assertEqual(text.feed(") 后文"), " 后文")
            self.assertEqual(text.feed("", final=True), "")
            text = ImageTextFilter(access)
            external = "![外链](https://external.example/a.png)"
            self.assertEqual(text.feed(external), external)
            own = "![图](https://owui.example/api/v1/files/id/content)"
            self.assertEqual(text.feed(own), "")
            self.assertEqual(markdown_images("```\n" + own + "\n```"), [])
            self.assertEqual(markdown_images(own), ["https://owui.example/api/v1/files/id/content"])
        finally:
            await client.close()

    async def test_extracts_file_and_structured_images_without_tool_arguments(self):
        value = {
            "files": [{"type": "image", "url": "/api/v1/files/id/content"}],
            "output": [{"type": "message", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]}],
            "arguments": {"type": "image", "url": "https://secret.example/private"},
        }
        self.assertEqual(image_sources(value), [
            "/api/v1/files/id/content", "data:image/png;base64,AAAA",
        ])
        self.assertEqual(image_sources({
            "type": "function_call_output", "output": "![图](/api/v1/files/id/content)",
        }), ["/api/v1/files/id/content"])
        self.assertEqual(image_sources({
            "type": "function_call_output",
            "output": json.dumps({"files": [{"type": "image", "url": "/api/v1/files/id/content"}]}),
        }), ["/api/v1/files/id/content"])

    async def test_image_paths_work_with_a_private_backend_and_reverse_proxy_prefix(self):
        client = OwuiClient("http://172.20.0.2:8080/prefix")
        try:
            access = OwuiImages(client, MediaConfig())
            self.assertEqual(
                access.owned_url("/api/v1/files/id/content"),
                "http://172.20.0.2:8080/prefix/api/v1/files/id/content",
            )
            self.assertFalse(access.supported("https://public-frontend.example/api/v1/files/id/content"))
        finally:
            await client.close()

    async def test_deduplicates_events_and_final_output_and_does_not_fetch_external_images(self):
        client = OwuiClient("https://owui.example")
        image = picture()
        inbox = SimpleNamespace(settings=MediaConfig(), valid=lambda uid, scope: True)
        send_image = AsyncMock()
        send_text = AsyncMock()
        runner = ImageChatRunner(
            None, None, client, None, send_text,
            inbox=inbox, scope="scope", send_image=send_image,
        )
        source = image.inline()
        runner.observe_event({"data": {"type": "files", "data": {
            "files": [{"type": "image", "url": source}],
        }}})
        runner.observe_event({"data": {"type": "files", "data": {
            "files": [{"type": "image", "url": source}],
        }}})
        renderer = SimpleNamespace(
            full_text=f"![图]({source}) ![外链](https://external.example/pic)",
            output=[{"type": "image_url", "image_url": {"url": source}}],
        )
        result = TurnResult()
        try:
            await runner.after_stream("alice", "fixture-token", renderer, result)
            self.assertEqual(send_image.await_count, 1)
            self.assertEqual(result.sent_segments, 1)
            send_text.assert_not_awaited()
        finally:
            await client.close()

    async def test_outbound_failure_and_size_limit_preserve_text_result(self):
        client = OwuiClient("https://owui.example")
        inbox = SimpleNamespace(settings=MediaConfig(max_batch_bytes=1), valid=lambda uid, scope: True)
        send_image, send_text = AsyncMock(), AsyncMock()
        runner = ImageChatRunner(
            None, None, client, None, send_text,
            inbox=inbox, scope="scope", send_image=send_image,
        )
        result = TurnResult()
        result.sent_segments = 2
        renderer = SimpleNamespace(full_text=f"正文 ![图]({picture().inline()})", output=None)
        try:
            await runner.after_stream("alice", "token", renderer, result)
            send_image.assert_not_awaited()
            self.assertIsNone(result.error)
            self.assertEqual(result.sent_segments, 3)
            self.assertIn("网页", send_text.await_args.args[1])
        finally:
            await client.close()

    async def test_temporary_history_keeps_image_content_parts(self):
        files = [{"type": "image", "url": picture().inline()}]
        history = [{"role": "user", "content": "上一题", "files": files},
                   {"role": "assistant", "content": "回答"}]
        messages = _serialize_temporary_history(history, "这一题", files)
        self.assertEqual(messages[0]["content"][1]["type"], "image_url")
        self.assertEqual(messages[-1]["content"][1]["image_url"]["url"], files[0]["url"])
        self.assertEqual(messages[1]["content"], "回答")


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.config = ConfigStore(directory)
        self.config.cfg.reply.segment_interval = 0
        self.state = StateStore(self.config.cfg.state_path)
        self.state.save_login({
            "bot_id": "bot-alice", "bot_token": "fixture-bot", "scanner_user_id": "wx-alice",
            "base_url": "https://api.wechat.example",
        })
        self.state.upsert_binding("wx-alice", {
            "email": "alice@example.com", "password": "", "owui_user_id": "alice",
            "owui_name": "Alice", "jwt_token": "fixture-jwt", "jwt_expires_at": None,
        })
        self.state.set_focus("wx-alice", model_id="vision")
        self.client = OwuiClient("https://owui.example")
        self.client.list_models = AsyncMock(return_value=[{"id": "vision", "info": None}])
        self.inbox = ImageInbox(directory / "media", MediaConfig())
        self.inbox.weixin.download = AsyncMock(return_value=picture())
        self.adapter = WebAdapter(
            self.config.cfg, self.state, None, self.client,
            access_lock=asyncio.Lock(), media=self.inbox,
        )
        self.handle = SimpleNamespace(
            account_id="bot-alice", token="fixture-bot", client=SimpleNamespace(),
        )
        self.adapter._accounts["bot-alice"] = self.handle
        self.adapter._user_account["wx-alice"] = "bot-alice"
        self.adapter.send_text = AsyncMock()
        self.adapter._typing_start = AsyncMock()
        self.adapter._typing_stop = AsyncMock()
        self.adapter.runtimes.ensure_socket = AsyncMock(return_value=SimpleNamespace(sid="fixture-sid"))
        self.inbox.attach(self.adapter)
        self.calls = []
        self.result_error = None
        self.wait = None
        self.wait_for_uid = None
        self.generation_mode = None
        owner = self

        class FixtureRunner:
            def __init__(self, *args, **kwargs):
                self.generation_state = kwargs["generation_state"]

            async def run_turn(self, uid, token, text, *, files=None):
                owner.calls.append((uid, text, files))
                if owner.generation_mode is not None:
                    self.generation_state(owner.generation_mode)
                if owner.wait and (owner.wait_for_uid is None or owner.wait_for_uid == uid):
                    await owner.wait.wait()
                result = TurnResult()
                result.accepted = True
                result.error = owner.result_error
                result.ok = not result.error
                result.sent_segments = 1
                return result

        self.runner_patch = patch("oui_wechat.adapter.ImageChatRunner", FixtureRunner)
        self.runner_patch.start()

        async def upload(token, image, name):
            return {"id": str(uuid.uuid4()), "type": "image",
                    "url": "https://owui.example/api/v1/files/fixture/content"}

        self.upload_patch = patch("oui_wechat.image_inbox.OwuiImages.upload", side_effect=upload)
        self.upload_mock = self.upload_patch.start()
        self.sequence = 0

    async def asyncTearDown(self):
        rt = self.adapter.runtimes.get("wx-alice")
        rt.stopped = True
        if rt.current_task:
            rt.current_task.cancel()
            await asyncio.gather(rt.current_task, return_exceptions=True)
        self.runner_patch.stop()
        self.upload_patch.stop()
        await self.inbox.close()
        await self.client.close()
        self.state.close()
        self.temporary.cleanup()

    async def dispatch(self, text=None, images=0, message_id=None):
        self.sequence += 1
        items = [{"type": 2, "image_item": {"media": {"encrypt_query_param": "fixture"}}}
                 for _ in range(images)]
        if text is not None:
            items.append({"type": 1, "text_item": {"text": text}})
        await self.adapter._dispatch(self.handle, {
            "message_type": 1, "from_user_id": "wx-alice", "context_token": "fixture-context",
            "message_id": message_id or self.sequence, "item_list": items,
        })

    async def drain(self):
        task = self.adapter.runtimes.get("wx-alice").current_task
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)

    async def run_real_worker(self, *, images=0, generation=False):
        # Exercise the production constructor and callback, not the permissive queue-test double.
        self.runner_patch.stop()
        self.config.cfg.capabilities.image_generation = generation
        queue = asyncio.Queue()
        queue.put_nowait({"data": {"type": "chat:completion", "data": {
            "done": True, "output": [{
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "真实聊天类回归回答"},
                ],
            }],
        }}})
        socket = SimpleNamespace(
            sid="fixture-sid", subscribe=lambda *args: queue, unsubscribe=lambda *args: None,
        )
        self.adapter.runtimes.ensure_socket.return_value = socket

        async def start(token, body):
            self.assertEqual(self.adapter.image_busy("wx-alice"), generation)
            if generation:
                await self.dispatch("生图期间不应排队")
                self.assertEqual(self.adapter.runtimes.get("wx-alice").pending, [])
            return {"status": True, "chat_id": "real-worker-chat"}

        self.client.start_chat_completion = AsyncMock(side_effect=start)
        self.client.get_chat = AsyncMock(return_value={"chat": {"history": {"messages": {}}}})
        with patch("open_webui_weixin.chat.capabilities.fetch_and_resolve",
                   AsyncMock(return_value=RequestCaps(features={"image_generation": generation}))):
            if images:
                await self.dispatch(images=images)
            await self.dispatch("真实调用链问题")
            await self.drain()
        self.client.start_chat_completion.assert_awaited_once()
        body = self.client.start_chat_completion.await_args.args[1]
        self.assertEqual(body["user_message"]["content"], "真实调用链问题")
        self.assertEqual(len(body["user_message"].get("files", [])), images)
        replies = [call.args[1] for call in self.adapter.send_text.await_args_list]
        self.assertTrue(any("真实聊天类回归回答" in text for text in replies))
        self.assertFalse(any("本轮处理未完成" in text for text in replies))
        self.assertEqual(self.state.get_focus("wx-alice")["chat_id"], "real-worker-chat")
        self.assertFalse(self.adapter.image_busy("wx-alice"))

    async def test_real_image_runner_worker_handles_plain_text_with_media_enabled(self):
        await self.run_real_worker()

    async def test_real_image_runner_worker_handles_image_and_text(self):
        await self.run_real_worker(images=1)

    async def test_real_image_runner_worker_keeps_generation_callback_and_rejection(self):
        await self.run_real_worker(generation=True)

    async def test_image_only_waits_and_multiple_images_acknowledge_once(self):
        await self.dispatch(images=1)
        await self.dispatch(images=1)
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.adapter.send_text.await_count, 1)
        self.assertIn("继续发送问题", self.adapter.send_text.await_args.args[1])
        batch = self.inbox.store.db.execute("SELECT id FROM batches").fetchone()
        self.assertEqual(len(self.inbox.store.images(batch["id"])), 2)

    async def test_question_waits_for_its_images_and_uploads_as_one_turn(self):
        ready = asyncio.Event()

        async def download(*args):
            await ready.wait()
            return picture()

        self.inbox.weixin.download.side_effect = download
        await self.dispatch(images=2)
        await self.dispatch("比较这两张图片")
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls, [])
        ready.set()
        await self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0][2]), 2)
        self.assertEqual(self.upload_mock.await_count, 2)
        self.assertEqual(self.inbox.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)

    async def test_plain_text_starts_without_a_merge_timer_and_later_image_waits(self):
        await self.dispatch("立即回答")
        self.assertIsNotNone(self.adapter.runtimes.get("wx-alice").current_task)
        await self.dispatch(images=1)
        await self.drain()
        self.assertEqual([(text, files) for _, text, files in self.calls], [("立即回答", None)])
        self.assertEqual(self.inbox.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 1)

    async def test_backend_error_does_not_echo_tokens_or_inline_images_to_wechat(self):
        self.result_error = (
            "failed jwt_token='private-jwt' aes_key='private-aes' "
            "data:image/png;base64,AAAA https://example.com/error?token=private-param"
        )
        await self.dispatch("问题")
        await self.drain()
        text = "\n".join(call.args[1] for call in self.adapter.send_text.await_args_list)
        for secret in ("private-jwt", "private-aes", "AAAA", "private-param"):
            self.assertNotIn(secret, text)

    async def test_queued_batches_keep_their_boundaries(self):
        self.wait = asyncio.Event()
        await self.dispatch(images=1)
        await self.dispatch("第一题")
        await asyncio.sleep(0.01)
        await self.dispatch(images=2)
        await self.dispatch("第二题")
        await self.dispatch(images=1)
        self.wait.set()
        await self.drain()
        self.assertEqual([text for _, text, _ in self.calls], ["第一题", "第二题"])
        self.assertEqual([len(files) for _, _, files in self.calls], [1, 2])
        batch = self.inbox.store.db.execute("SELECT id FROM batches").fetchone()
        self.assertEqual(len(self.inbox.store.images(batch["id"])), 1)

    async def test_duplicate_message_does_not_add_or_submit_twice(self):
        await self.dispatch(images=1, message_id="image-1")
        await self.dispatch(images=1, message_id="image-1")
        await self.dispatch("问题", message_id="question-1")
        await self.dispatch("问题", message_id="question-1")
        await self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0][2]), 1)

    async def test_image_generation_rejects_text_images_and_commands_without_queueing(self):
        self.config.cfg.capabilities.image_generation = True
        self.wait = asyncio.Event()
        await self.dispatch("画一幅图")
        # The preflight window is protected too, before the worker has run.
        await self.dispatch("再加一句")
        await asyncio.sleep(0.01)
        focus = dict(self.state.get_focus("wx-alice"))
        await self.dispatch(images=1)
        await self.dispatch("/chat new")
        await self.dispatch("/model use 1")
        rt = self.adapter.runtimes.get("wx-alice")
        self.assertEqual(rt.pending, [])
        self.assertFalse(rt.stopped)
        self.assertEqual(dict(self.state.get_focus("wx-alice")), focus)
        self.assertEqual(self.inbox.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        notices = [call.args[1] for call in self.adapter.send_text.await_args_list]
        self.assertEqual(sum("本条消息未接收" in text for text in notices), 4)
        self.wait.set()
        await self.drain()
        self.assertFalse(self.adapter.image_busy("wx-alice"))
        await self.dispatch("完成后的问题")
        await self.drain()
        self.assertEqual([text for _, text, _ in self.calls], ["画一幅图", "完成后的问题"])

    async def test_stop_is_still_available_during_image_generation(self):
        self.config.cfg.capabilities.image_generation = True
        self.wait = asyncio.Event()
        await self.dispatch("画图")
        await asyncio.sleep(0.01)
        await self.dispatch("/stop")
        self.assertFalse(self.adapter.image_busy("wx-alice"))
        self.assertEqual(self.adapter.runtimes.get("wx-alice").pending, [])

    async def test_non_image_model_releases_preflight_guard_and_preserves_text_queue(self):
        self.config.cfg.capabilities.image_generation = True
        self.generation_mode = False
        self.wait = asyncio.Event()
        await self.dispatch("普通文字")
        await asyncio.sleep(0.01)
        await self.dispatch("第二句")
        self.assertEqual(len(self.adapter.runtimes.get("wx-alice").pending), 1)
        self.wait.set()
        await self.drain()
        self.assertEqual([text for _, text, _ in self.calls], ["普通文字", "第二句"])

    async def test_detected_image_tool_blocks_only_its_user_even_with_feature_disabled(self):
        self.generation_mode = True
        self.wait = asyncio.Event()
        await self.dispatch("调用图片工具")
        await asyncio.sleep(0.01)
        await self.dispatch("不要插队")
        self.assertEqual(self.adapter.runtimes.get("wx-alice").pending, [])
        self.assertFalse(self.adapter.image_busy("wx-bob"))
        self.wait.set()
        await self.drain()

    async def test_download_failure_retains_batch_for_question_retry(self):
        self.inbox.weixin.download.side_effect = MediaError("fixture download error")
        await self.dispatch(images=1)
        await self.dispatch("问题")
        await self.drain()
        self.assertEqual(self.calls, [])
        batch = self.inbox.store.db.execute("SELECT * FROM batches").fetchone()
        self.assertEqual(batch["status"], "pending")
        self.inbox.weixin.download.side_effect = None
        await self.dispatch("问题重试")
        await self.drain()
        self.assertEqual(len(self.calls), 1)

    async def test_upload_failure_retains_batch_and_does_not_submit_text_alone(self):
        self.upload_mock.side_effect = MediaError("上传失败，请重试")
        await self.dispatch(images=1)
        await self.dispatch("问题")
        await self.drain()
        self.assertEqual(self.calls, [])
        batch = self.inbox.store.db.execute("SELECT * FROM batches").fetchone()
        self.assertEqual(batch["status"], "pending")
        self.upload_mock.side_effect = None
        self.upload_mock.return_value = {
            "type": "image", "url": "https://owui.example/api/v1/files/id/content",
        }
        await self.dispatch("重试问题")
        await self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0][2]), 1)

    async def test_expiration_notifies_once_and_does_not_call_model(self):
        await self.dispatch(images=1)
        self.inbox.store.db.execute("UPDATE batches SET expires=0")
        self.inbox.sweeper.cancel()
        await asyncio.gather(self.inbox.sweeper, return_exceptions=True)
        self.inbox.sweeper = asyncio.create_task(self.inbox._sweep())
        await asyncio.sleep(0.01)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.inbox.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        notices = [call.args[1] for call in self.adapter.send_text.await_args_list]
        self.assertEqual(sum("过期" in text for text in notices), 1)
        self.assertEqual(self.inbox.store.expire(), [])

    async def test_expiration_cleanup_continues_while_adapter_is_paused(self):
        await self.dispatch(images=1)
        await self.inbox.detach()
        self.inbox.store.db.execute("UPDATE batches SET expires=0")
        self.inbox.sweeper.cancel()
        await asyncio.gather(self.inbox.sweeper, return_exceptions=True)
        self.inbox.start_cleanup()
        await asyncio.sleep(0.01)
        self.assertEqual(self.inbox.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        self.assertEqual(self.calls, [])

    async def test_second_user_text_completes_while_first_users_images_are_downloading(self):
        wait = asyncio.Event()

        async def download(*args):
            await wait.wait()
            return picture()

        self.inbox.weixin.download.side_effect = download
        self.state.save_login({
            "bot_id": "bot-bob", "bot_token": "fixture-bob", "scanner_user_id": "wx-bob",
            "base_url": "https://api.wechat.example",
        })
        self.state.upsert_binding("wx-bob", {
            "email": "bob@example.com", "password": "", "owui_user_id": "bob",
            "owui_name": "Bob", "jwt_token": "fixture-bob", "jwt_expires_at": None,
        })
        self.state.set_focus("wx-bob", model_id="vision")
        bob = SimpleNamespace(account_id="bot-bob", token="fixture-bob", client=SimpleNamespace())
        self.adapter._accounts["bot-bob"] = bob
        await self.dispatch(images=1)
        await self.dispatch("Alice 图片问题")
        await self.adapter._dispatch(bob, {
            "message_type": 1, "from_user_id": "wx-bob", "context_token": "fixture-bob-context",
            "message_id": "bob-question", "item_list": [{"type": 1, "text_item": {"text": "Bob 文字问题"}}],
        })
        await asyncio.wait_for(self.adapter.runtimes.get("wx-bob").current_task, timeout=0.5)
        self.assertEqual(self.calls, [("wx-bob", "Bob 文字问题", None)])
        wait.set()
        await self.drain()
        self.assertEqual(self.calls[1][0], "wx-alice")
        self.assertEqual(len(self.calls[1][2]), 1)

    async def test_other_user_can_finish_text_while_first_user_is_generating_images(self):
        self.config.cfg.capabilities.image_generation = True
        self.wait = asyncio.Event()
        self.wait_for_uid = "wx-alice"
        await self.dispatch("生成图片")
        await asyncio.sleep(0.01)
        self.state.save_login({
            "bot_id": "bot-bob", "bot_token": "fixture-bob", "scanner_user_id": "wx-bob",
            "base_url": "https://api.wechat.example",
        })
        self.state.upsert_binding("wx-bob", {
            "email": "bob@example.com", "password": "", "owui_user_id": "bob",
            "owui_name": "Bob", "jwt_token": "fixture-bob", "jwt_expires_at": None,
        })
        self.state.set_focus("wx-bob", model_id="vision")
        bob = SimpleNamespace(account_id="bot-bob", token="fixture-bob", client=SimpleNamespace())
        self.adapter._accounts["bot-bob"] = bob
        await self.adapter._dispatch(bob, {
            "message_type": 1, "from_user_id": "wx-bob", "context_token": "fixture-bob-context",
            "message_id": "bob-question", "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
        })
        await asyncio.wait_for(self.adapter.runtimes.get("wx-bob").current_task, timeout=0.5)
        self.assertTrue(self.adapter.image_busy("wx-alice"))
        self.assertEqual(self.calls[-1], ("wx-bob", "你好", None))
        self.wait.set()
        await self.drain()

    async def test_nonvision_model_does_not_submit_or_upload_and_can_be_changed(self):
        self.client.list_models.return_value = [{
            "id": "vision", "info": {"meta": {"capabilities": {"vision": False}}},
        }]
        await self.dispatch(images=1)
        await self.dispatch("问题")
        await self.drain()
        self.assertEqual(self.calls, [])
        self.upload_mock.assert_not_awaited()
        self.client.list_models.return_value = [{"id": "vision", "info": None}]
        await self.dispatch("换模型后重试")
        await self.drain()
        self.assertEqual(len(self.calls), 1)

    async def test_new_chat_and_stop_clear_pending_images(self):
        await self.dispatch(images=1)
        await self.dispatch("/chat new")
        self.assertEqual(self.inbox.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)
        await self.dispatch(images=1)
        await self.dispatch("/stop")
        self.assertEqual(self.inbox.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)

    async def test_changed_owner_cannot_consume_previous_users_images(self):
        await self.dispatch(images=1)
        self.state.upsert_binding("wx-alice", {
            "email": "bob@example.com", "password": "", "owui_user_id": "bob",
            "owui_name": "Bob", "jwt_token": "fixture-bob", "jwt_expires_at": None,
        })
        await self.dispatch("新的用户问题")
        await self.drain()
        self.assertEqual(self.calls[0][2], None)
        self.upload_mock.assert_not_awaited()

    async def test_temporary_turn_uses_inline_images_and_clears_local_batch(self):
        self.state.set_focus("wx-alice", temporary=1)
        await self.dispatch(images=1)
        await self.dispatch("临时问题")
        await self.drain()
        self.assertTrue(self.calls[0][2][0]["url"].startswith("data:image/"))
        self.upload_mock.assert_not_awaited()
        self.assertEqual(list(self.inbox.store.directory.glob("*.image")), [])

    async def test_slow_media_never_holds_global_account_lock(self):
        wait = asyncio.Event()

        async def download(*args):
            await wait.wait()
            return picture()

        self.inbox.weixin.download.side_effect = download
        await self.dispatch(images=1)
        await self.dispatch("图片问题")
        async with asyncio.timeout(0.2):
            async with self.adapter.access_lock:
                pass
        wait.set()
        await self.drain()


class ChatAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_feature_enables_guard_before_backend_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigStore(Path(directory))
            state = StateStore(config.cfg.state_path)
            state.set_focus("user", model_id="model")
            events = []
            client = SimpleNamespace(start_chat_completion=AsyncMock(return_value={"status": False}))
            runner = ChatRunner(
                config.cfg, state, client,
                SimpleNamespace(sid="sid", subscribe=lambda *args: None, unsubscribe=lambda *args: None),
                AsyncMock(), generation_state=events.append,
            )
            try:
                with patch("open_webui_weixin.chat.capabilities.fetch_and_resolve",
                           AsyncMock(return_value=RequestCaps(features={"image_generation": True}))):
                    await runner.run_turn("user", "fixture", "画图")
                self.assertEqual(events, [True])
                self.assertTrue(runner.image_generation)
            finally:
                state.close()

    async def test_selected_model_is_rechecked_after_image_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigStore(Path(directory))
            state = StateStore(config.cfg.state_path)
            state.set_focus("user", model_id="new-nonvision-model")
            client = SimpleNamespace(
                base_url="https://owui.example",
                list_models=AsyncMock(return_value=[{
                    "id": "new-nonvision-model", "info": {"meta": {"capabilities": {"vision": False}}},
                }]),
                start_chat_completion=AsyncMock(),
            )
            inbox = SimpleNamespace(settings=MediaConfig(), valid=lambda uid, scope: True)
            runner = ImageChatRunner(
                config.cfg, state, client, SimpleNamespace(sid="sid"), AsyncMock(),
                inbox=inbox, scope="scope", send_image=AsyncMock(),
            )
            try:
                result = await runner.run_turn("user", "fixture", "问题", files=[{
                    "type": "image", "url": picture().inline(),
                }])
                self.assertIn("不支持图片", result.error)
                client.start_chat_completion.assert_not_awaited()
            finally:
                state.close()

    async def test_user_message_files_are_saved_with_the_text_and_temp_history_keeps_them(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigStore(Path(directory))
            config.cfg.reply.segment_interval = 0
            state = StateStore(config.cfg.state_path)
            state.set_focus("user", model_id="model")
            queue = asyncio.Queue()
            queue.put_nowait({"data": {"type": "chat:completion", "data": {"done": True, "content": "回答"}}})
            socket = SimpleNamespace(
                sid="sid", subscribe=lambda *args: queue, unsubscribe=lambda *args: None,
            )
            client = SimpleNamespace(start_chat_completion=AsyncMock(return_value={
                "status": True, "chat_id": "new-chat",
            }))
            runner = ChatRunner(config.cfg, state, client, socket, AsyncMock())
            files = [{"type": "image", "id": str(uuid.uuid4()), "url": picture().inline()}]
            try:
                with patch("open_webui_weixin.chat.capabilities.fetch_and_resolve",
                           AsyncMock(return_value=RequestCaps())):
                    result = await runner.run_turn("user", "fixture", "看图", files=files)
                body = client.start_chat_completion.await_args.args[1]
                self.assertEqual(body["user_message"]["content"], "看图")
                self.assertEqual(body["user_message"]["files"], files)
                self.assertTrue(result.accepted)
                state.temporary_append("user", "看图", "回答", files=files)
                self.assertEqual(state.temporary_history("user")[0]["files"], files)
                state.temporary_drop("user")
                self.assertEqual(state.temporary_history("user"), [])
            finally:
                state.close()


class GenerationTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def stream(self, delays, *, image=False, tool_event=False):
        clock = [0.0]
        timeouts = []
        config = SimpleNamespace(reply=SimpleNamespace(segment_interval=0))
        runner = ChatRunner(config, SimpleNamespace(), None, None, AsyncMock())
        runner.set_image_generation(image)
        queue = asyncio.Queue()
        if tool_event:
            queue.put_nowait({"data": {"type": "response:completion", "data": {
                "type": "response.output_item.added", "item": {
                    "type": "function_call", "name": "generate_image", "call_id": "fixture",
                },
            }}})
        for index in range(len(delays) - int(tool_event)):
            done = index == len(delays) - int(tool_event) - 1
            queue.put_nowait({"data": {"type": "chat:completion", "data": {
                "done": done, "content": "已完成" if done else "",
            }}})
        waits = iter(delays)

        async def wait_for(awaitable, *, timeout):
            timeouts.append(timeout)
            delay = next(waits)
            if delay > timeout:
                awaitable.close()
                clock[0] += timeout
                raise TimeoutError
            clock[0] += delay
            return await awaitable

        renderer = TurnRenderer(max_length=2048)
        result = TurnResult()
        with patch("open_webui_weixin.chat.asyncio", SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock[0]), wait_for=wait_for,
        )):
            await runner._stream_to_weixin("user", renderer, queue, result, "fixture")
        return result, timeouts

    async def test_seven_minute_silent_image_generation_completes(self):
        result, timeouts = await self.stream([420], image=True)
        self.assertIsNone(result.error)
        self.assertEqual(timeouts, [1800])

    async def test_image_generation_has_a_total_thirty_minute_bound(self):
        result, timeouts = await self.stream([900, 900, 1], image=True)
        self.assertIn("超时", result.error)
        self.assertEqual(timeouts, [1800, 900])

    async def test_silent_image_generation_still_times_out_after_thirty_minutes(self):
        result, timeouts = await self.stream([1801], image=True)
        self.assertIsNotNone(result.error)
        self.assertEqual(timeouts, [1800])

    async def test_plain_text_keeps_existing_idle_timeout(self):
        result, timeouts = await self.stream([181])
        self.assertIn("长时间无响应", result.error)
        self.assertEqual(timeouts, [180])

    async def test_image_tool_event_extends_wait_without_builtin_feature(self):
        result, timeouts = await self.stream([1, 420], tool_event=True)
        self.assertIsNone(result.error)
        self.assertEqual(timeouts, [180, 1799])


if __name__ == "__main__":
    unittest.main(verbosity=2)
