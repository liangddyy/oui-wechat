"""Image-aware chat hooks, preserving the adapter's existing streaming text renderer."""

from __future__ import annotations

import hashlib
import json
import logging

from markdown_it import MarkdownIt
from markdown_it.rules_inline.image import image as image_rule
from open_webui_weixin.chat import ChatRunner

from .image_transport import MediaError, OwuiImages

log = logging.getLogger(__name__)


async def require_vision(owui, token: str, model_id: str):
    models = await owui.list_models(token)
    model = next((item for item in models if item.get("id") == model_id), None)
    if model is None:
        raise MediaError("当前模型不可用，请选择可用的视觉模型后重新发送问题")
    meta = (model.get("info") or {}).get("meta") or {}
    if (meta.get("capabilities") or {}).get("vision") is False:
        raise MediaError("当前模型不支持图片，请切换视觉模型后重新发送问题")


def markdown_images(text: str) -> list[str]:
    result = []
    for token in MarkdownIt("commonmark").parse(text):
        for child in token.children or []:
            if child.type == "image":
                source = child.attrGet("src")
                if source:
                    result.append(source)
    return result


def image_sources(value) -> list[str]:
    """Read image-bearing fields only; never interpret tool arguments or arbitrary links."""
    result = []
    if isinstance(value, list):
        for child in value:
            result.extend(image_sources(child))
    elif isinstance(value, dict):
        kind = value.get("type", "")
        mime = value.get("content_type") or value.get("mime_type") or ""
        if kind in {"image", "image_url", "output_image", "input_image"} or str(mime).startswith("image/"):
            source = value.get("url") or value.get("image_url")
            if isinstance(source, dict):
                source = source.get("url")
            if isinstance(source, str):
                result.append(source)
            encoded = value.get("b64_json")
            if isinstance(encoded, str):
                result.append("data:image/png;base64," + encoded)
        if kind == "image_generation_call" and value.get("status") == "completed":
            encoded = value.get("result")
            if isinstance(encoded, str):
                result.append("data:image/png;base64," + encoded)
        for key in ("files", "images", "output", "content", "item"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                result.extend(image_sources(child))
            elif key == "content" and isinstance(child, str):
                result.extend(markdown_images(child))
            elif key == "output" and kind == "function_call_output" and isinstance(child, str):
                try:
                    parsed = json.loads(child)
                except ValueError:
                    result.extend(markdown_images(child))
                else:
                    if isinstance(parsed, (dict, list)):
                        result.extend(image_sources(parsed))
    return result


class ImageTextFilter:
    """Hold incomplete image syntax so base64 and private file URLs never spill into text chunks."""

    def __init__(self, access: OwuiImages):
        self.access = access
        self.buffer = ""
        self.markdown = MarkdownIt("commonmark")

        def tracked(state, silent):
            start = state.pos
            if not image_rule(state, silent):
                return False
            if not silent:
                state.tokens[-1].meta["span"] = (start, state.pos)
            return True

        self.markdown.inline.ruler.at("image", tracked)

    def feed(self, chunk: str, *, final: bool = False) -> str:
        self.buffer += chunk
        start = self.buffer.find("![")
        if start < 0:
            hold = 1 if self.buffer.endswith("!") and not final else 0
            ready = self.buffer[:-hold] if hold else self.buffer
            self.buffer = self.buffer[-hold:] if hold else ""
            return ready
        prefix, remaining = self.buffer[:start], self.buffer[start:]
        if ")" not in chunk and not final:
            # Avoid reparsing a growing multi-megabyte inline image on every streamed chunk.
            self.buffer = remaining
            return prefix
        tokens = self.markdown.parseInline(remaining)[0].children or []
        spans = []
        last_end = 0
        for token in tokens:
            if token.type == "image" and "span" in token.meta:
                lo, hi = token.meta["span"]
                last_end = max(last_end, hi)
                if self.access.supported(token.attrGet("src") or ""):
                    spans.append((lo, hi))
        end = len(remaining)
        unfinished = remaining.find("![", last_end)
        if unfinished >= 0 and not final:
            end = unfinished
        output, position = [prefix], 0
        for lo, hi in spans:
            if hi <= end:
                output.append(remaining[position:lo])
                position = hi
        output.append(remaining[position:end])
        self.buffer = remaining[end:]
        text = "".join(output)
        # Even malformed final image markup must not expose a base64 payload in WeChat.
        if final and "data:image/" in text:
            text = text[:text.index("data:image/")] + "（图片请在 OpenWebUI 查看）"
        return text


class ImageChatRunner(ChatRunner):
    def __init__(self, *args, inbox, scope: str, send_image, batch_id=None, generation_state=None):
        super().__init__(*args, generation_state=generation_state)
        self.inbox = inbox
        self.scope = scope
        self.send_image = send_image
        self.batch_id = batch_id
        self.access = OwuiImages(self.owui, inbox.settings)
        self.candidates: list[str] = []
        self.candidate_sources: set[str] = set()
        self.truncated = False
        self.filter = ImageTextFilter(self.access)

    def collect(self, sources):
        for source in sources:
            if source in self.candidate_sources or not self.access.supported(source):
                continue
            if (
                len(self.candidates) >= self.inbox.settings.max_images_per_turn * 2 + 1
                or len(source) > self.inbox.settings.max_image_bytes * 4 // 3 + 128
            ):
                self.truncated = True
                continue
            self.candidate_sources.add(source)
            self.candidates.append(source)

    def before_submit(self):
        if self.batch_id:
            self.inbox.store.db.execute(
                "UPDATE batches SET status='submitting' WHERE id=?", (self.batch_id,),
            )

    async def check_attachments(self, jwt_token, model_id, files):
        try:
            await require_vision(self.owui, jwt_token, model_id)
        except MediaError as exc:
            return str(exc)
        return None

    def on_accepted(self):
        if self.batch_id:
            self.inbox.store.drop(self.batch_id)

    def observe_event(self, payload: dict) -> None:
        super().observe_event(payload)
        outer = payload.get("data") or {}
        kind = outer.get("type")
        data = outer.get("data")
        if kind == "files":
            self.collect(image_sources(data))
        elif kind == "chat:completion" and isinstance(data, dict) and data.get("done"):
            self.collect(image_sources(data))
        elif kind == "response:completion" and isinstance(data, dict):
            event = data.get("type", "")
            if event in {"response.output_item.done", "response.completed"}:
                self.collect(image_sources(data))

    async def _emit(self, uid, chunks, result):
        if not self.inbox.valid(uid, self.scope):
            return
        cleaned = [self.filter.feed(chunk) for chunk in chunks]
        await super()._emit(uid, [chunk for chunk in cleaned if chunk.strip()], result)

    async def after_stream(self, uid, token, renderer, result):
        if not self.inbox.valid(uid, self.scope):
            return
        tail = self.filter.feed("", final=True)
        if tail.strip():
            await super()._emit(uid, [tail], result)
        self.collect(markdown_images(renderer.full_text))
        self.collect(image_sources(renderer.output))
        # Include final stored attachments, which some tools do not emit as streaming events.
        if result.chat_id and not result.chat_id.startswith("temporary:"):
            try:
                detail = await self.owui.get_chat(token, result.chat_id)
                chat = detail.get("chat", detail)
                node = chat.get("history", {}).get("messages", {}).get(result.assistant_message_id, {})
                self.collect(image_sources(node))
            except Exception as exc:
                log.info("读取最终图片附件未完成：%s", type(exc).__name__)
        sent, size, warning = 0, 0, self.truncated
        seen_sources, seen_images = set(), set()
        for source in self.candidates:
            if source in seen_sources or not self.access.supported(source):
                continue
            seen_sources.add(source)
            if not self.inbox.valid(uid, self.scope):
                return
            try:
                image = await self.access.fetch(token, source)
                digest = hashlib.sha256(image.data).hexdigest()
                if digest in seen_images:
                    continue
                seen_images.add(digest)
                if (
                    sent >= self.inbox.settings.max_images_per_turn
                    or size + len(image.data) > self.inbox.settings.max_batch_bytes
                ):
                    warning = True
                    break
                await self.send_image(uid, self.scope, image)
                sent += 1
                size += len(image.data)
                result.sent_segments += 1
            except Exception as exc:
                if not isinstance(exc, MediaError):
                    log.info("图片回传未完成：%s", type(exc).__name__)
                warning = True
        if warning and self.inbox.valid(uid, self.scope):
            await self._send_text(uid, "部分图片未能发送或超过上限，请在 OpenWebUI 网页查看。")
            result.sent_segments += 1
