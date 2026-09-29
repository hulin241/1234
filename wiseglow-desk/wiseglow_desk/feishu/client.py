"""Async-friendly wrapper around the synchronous lark-oapi REST client.

Every SDK call is synchronous and runs in a worker thread through
``asyncio.to_thread``. ``send_*`` methods never raise: a failed send is
logged and yields ``None``. Lower-level helpers (``upload_file``,
``download_resource``) raise :class:`FeishuError`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .content import card_content, looks_like_markdown, markdown_card, split_text, text_content

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
SAFE_NAME_MAX_LEN = 80
BOT_INFO_URI = "/open-apis/bot/v3/info"

# Feishu file_type by extension; the second item is the fallback to retry
# with when the first one is rejected (docx/xlsx/pptx are not enumerated).
_FILE_TYPES: dict[str, tuple[str, str | None]] = {
    ".pdf": ("pdf", None),
    ".doc": ("doc", None),
    ".xls": ("xls", None),
    ".ppt": ("ppt", None),
    ".mp4": ("mp4", None),
    ".opus": ("opus", None),
    ".docx": ("doc", "stream"),
    ".xlsx": ("xls", "stream"),
    ".pptx": ("ppt", "stream"),
}

_UNSAFE_CHARS_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


class FeishuError(Exception):
    """A non-success response (or local precondition failure) from Feishu."""

    def __init__(self, code: int | None, msg: str, log_id: str | None = None) -> None:
        self.code = code
        self.msg = msg
        self.log_id = log_id
        detail = f"Feishu error {code}: {msg}" if code is not None else f"Feishu: {msg}"
        if log_id:
            detail += f" (log_id={log_id})"
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without network)
# ---------------------------------------------------------------------------


def resolve_domain(domain: str | None) -> str:
    """Map ``"feishu"`` / ``"lark"`` (or a full https URL) to the SDK domain."""
    key = (domain or "feishu").strip().lower().rstrip("/")
    if key in ("feishu", lark.FEISHU_DOMAIN):
        return lark.FEISHU_DOMAIN
    if key in ("lark", "larksuite", lark.LARK_DOMAIN):
        return lark.LARK_DOMAIN
    if key.startswith("https://"):
        return key
    raise ValueError(f"FEISHU_DOMAIN non valido: {domain!r} (usa 'feishu' oppure 'lark')")


def file_type_for(path: Path) -> tuple[str, str | None]:
    """Return ``(file_type, fallback)`` for the upload API by extension."""
    return _FILE_TYPES.get(path.suffix.lower(), ("stream", None))


def safe_filename(name: str, max_len: int = SAFE_NAME_MAX_LEN) -> str:
    """Sanitise a file name: keep letters/digits (incl. CJK), ``._-``; cap length."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    stem = _UNSAFE_CHARS_RE.sub("_", stem).strip("._-")
    stem = re.sub(r"_{2,}", "_", stem)
    ext = _UNSAFE_CHARS_RE.sub("", ext)[:10]
    if not stem:
        stem = "file"
    room = max(1, max_len - len(ext))
    return f"{stem[:room]}{ext}"


def timestamped_name(name: str, now: datetime | None = None) -> str:
    """``<YYYYMMDD-HHMMSS>_<safe_name>`` as used in the inbox folder."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{safe_filename(name)}"


def unique_path(directory: Path, filename: str) -> Path:
    """Return ``directory/filename`` or a ``-N`` suffixed variant that is free."""
    candidate = directory / filename
    stem, ext = os.path.splitext(filename)
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{ext}"
        counter += 1
    return candidate


def check_response(resp: lark.BaseResponse, action: str) -> None:
    """Raise :class:`FeishuError` (after logging) when ``resp`` is not a success."""
    if resp.success():
        return
    log_id = resp.get_log_id()
    log.error("Feishu %s failed: code=%s msg=%s log_id=%s", action, resp.code, resp.msg, log_id)
    raise FeishuError(resp.code, resp.msg or "errore sconosciuto", log_id)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class FeishuClient:
    """Thin async facade over ``lark_oapi.Client`` for a single bot app."""

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu") -> None:
        self.app_id = app_id
        self.domain = resolve_domain(domain)
        self._client: lark.Client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(self.domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self.sent_message_ids: deque[str] = deque(maxlen=500)
        self.bot_name: str | None = None
        self.last_error: str | None = None  # reason of the last failed call (for --check)
        self._bot_open_id: str | None = None

    # -- infrastructure ----------------------------------------------------

    async def _call(self, fn: Callable[..., T], *args: Any, action: str) -> T | None:
        """Run a sync SDK call in a thread; swallow (and log) every failure."""
        try:
            result = await asyncio.to_thread(fn, *args)
        except FeishuError as exc:
            self.last_error = str(exc)  # already logged by check_response / the caller
            return None
        except Exception as exc:  # noqa: BLE001 - the bot must survive any SDK error
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("Feishu %s failed: %s", action, self.last_error)
            log.debug("Feishu %s traceback", action, exc_info=True)
            return None
        self.last_error = None
        return result

    # -- bot identity ------------------------------------------------------

    def _bot_info_sync(self) -> dict[str, Any]:
        req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(BOT_INFO_URI)
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        resp = self._client.request(req)
        check_response(resp, "bot info")
        payload = json.loads(resp.raw.content or b"{}")
        bot = payload.get("bot") or {}  # top-level key is "bot", not "data"
        if not bot.get("open_id"):
            raise FeishuError(None, "risposta bot/v3/info senza open_id")
        return bot

    async def bot_open_id(self) -> str | None:
        """The bot's own open_id (cached); ``None`` when the lookup fails."""
        if self._bot_open_id:
            return self._bot_open_id
        info = await self._call(self._bot_info_sync, action="bot info")
        if info:
            self._bot_open_id = str(info.get("open_id"))
            self.bot_name = info.get("app_name") or self.bot_name
            log.info("Feishu bot identity: %s (%s)", self.bot_name, self._bot_open_id)
        return self._bot_open_id

    # -- sending messages --------------------------------------------------

    def _send_sync(self, chat_id: str, msg_type: str, content: str, reply_to: str | None) -> str | None:
        if reply_to:
            body = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content).reply_in_thread(False).build()
            req = ReplyMessageRequest.builder().message_id(reply_to).request_body(body).build()
            resp: Any = self._client.im.v1.message.reply(req)
            check_response(resp, "reply message")
        else:
            body = CreateMessageRequestBody.builder().receive_id(chat_id).msg_type(msg_type).content(content).build()
            req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            resp = self._client.im.v1.message.create(req)
            check_response(resp, "create message")
        message_id = resp.data.message_id if resp.data is not None else None
        if message_id:
            self.sent_message_ids.append(message_id)
        return message_id

    async def _send(self, chat_id: str, msg_type: str, content: str, *, reply_to: str | None = None) -> str | None:
        """Send one message; on a failed reply, retry once as a plain message."""
        message_id = await self._call(self._send_sync, chat_id, msg_type, content, reply_to, action="send")
        if message_id is None and reply_to:
            log.warning("Feishu reply to %s failed, sending without reply", reply_to)
            message_id = await self._call(self._send_sync, chat_id, msg_type, content, None, action="send")
        return message_id

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        """Send text (as a markdown card when it looks like Markdown), split if long.

        Only the first chunk is a reply to ``reply_to``. Returns the id of the
        last message sent, or ``None`` when nothing was sent.
        """
        chunks = split_text(text)
        if not chunks:
            return None
        as_card = looks_like_markdown(text)
        last_id: str | None = None
        for index, chunk in enumerate(chunks):
            if as_card:
                msg_type, payload = "interactive", card_content(markdown_card(chunk))
            else:
                msg_type, payload = "text", text_content(chunk)
            message_id = await self._send(chat_id, msg_type, payload, reply_to=reply_to if index == 0 else None)
            if message_id is None:
                return None
            last_id = message_id
        return last_id

    async def send_card(self, chat_id: str, card: dict[str, Any], *, reply_to: str | None = None) -> str | None:
        return await self._send(chat_id, "interactive", card_content(card), reply_to=reply_to)

    # -- files and images --------------------------------------------------

    def _upload_once(self, path: Path, file_type: str) -> str:
        with path.open("rb") as fh:  # keep the handle open until the call returns
            body = CreateFileRequestBody.builder().file_type(file_type).file_name(path.name).file(fh).build()
            resp = self._client.im.v1.file.create(CreateFileRequest.builder().request_body(body).build())
        check_response(resp, f"upload file ({file_type})")
        if resp.data is None or not resp.data.file_key:
            raise FeishuError(None, "upload senza file_key")
        return resp.data.file_key

    def _upload_file_sync(self, path: Path) -> str:
        size = path.stat().st_size
        if size == 0:
            raise FeishuError(None, f"file vuoto: {path.name}")
        if size > MAX_FILE_BYTES:
            raise FeishuError(None, f"file troppo grande ({size / 1_048_576:.1f} MB, limite 30 MB): {path.name}")
        file_type, fallback = file_type_for(path)
        try:
            return self._upload_once(path, file_type)
        except FeishuError:
            if fallback is None:
                raise
            log.warning("Feishu rejected file_type=%s for %s, retrying as %s", file_type, path.name, fallback)
            return self._upload_once(path, fallback)

    async def upload_file(self, path: Path) -> str:
        """Upload a file and return its ``file_key``. Raises :class:`FeishuError`."""
        try:
            return await asyncio.to_thread(self._upload_file_sync, Path(path))
        except FeishuError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Feishu upload of %s failed", path)
            raise FeishuError(None, f"upload fallito: {exc}") from exc

    async def send_file(self, chat_id: str, path: Path, *, reply_to: str | None = None) -> str | None:
        """Upload ``path`` and send it as a file message; ``None`` on failure."""
        try:
            file_key = await self.upload_file(path)
        except FeishuError as exc:
            log.error("Feishu send_file %s: %s", path, exc)
            return None
        return await self._send(chat_id, "file", json.dumps({"file_key": file_key}), reply_to=reply_to)

    def _upload_image_sync(self, path: Path) -> str:
        size = path.stat().st_size
        if size == 0 or size > MAX_IMAGE_BYTES:
            raise FeishuError(None, f"immagine vuota o troppo grande (limite 10 MB): {path.name}")
        with path.open("rb") as fh:
            body = CreateImageRequestBody.builder().image_type("message").image(fh).build()
            resp = self._client.im.v1.image.create(CreateImageRequest.builder().request_body(body).build())
        check_response(resp, "upload image")
        if resp.data is None or not resp.data.image_key:
            raise FeishuError(None, "upload immagine senza image_key")
        return resp.data.image_key

    async def send_image(self, chat_id: str, path: Path, *, reply_to: str | None = None) -> str | None:
        """Upload ``path`` and send it as an image message; ``None`` on failure."""
        image_key = await self._call(self._upload_image_sync, Path(path), action="upload image")
        if image_key is None:
            return None
        return await self._send(chat_id, "image", json.dumps({"image_key": image_key}), reply_to=reply_to)

    # -- reactions ---------------------------------------------------------

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        emoji = Emoji.builder().emoji_type(emoji_type).build()
        body = CreateMessageReactionRequestBody.builder().reaction_type(emoji).build()
        req = CreateMessageReactionRequest.builder().message_id(message_id).request_body(body).build()
        resp = self._client.im.v1.message_reaction.create(req)
        check_response(resp, "add reaction")
        return resp.data.reaction_id if resp.data is not None else None

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str | None:
        """Add an emoji reaction; returns the ``reaction_id`` (``None`` on failure)."""
        return await self._call(self._add_reaction_sync, message_id, emoji_type, action="add reaction")

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        req = DeleteMessageReactionRequest.builder().message_id(message_id).reaction_id(reaction_id).build()
        check_response(self._client.im.v1.message_reaction.delete(req), "remove reaction")

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        if not reaction_id:
            return
        await self._call(self._remove_reaction_sync, message_id, reaction_id, action="remove reaction")

    # -- downloads ---------------------------------------------------------

    def _download_sync(
        self, message_id: str, key: str, kind: str, dest_dir: Path, name_hint: str
    ) -> Path:
        req = GetMessageResourceRequest.builder().message_id(message_id).file_key(key).type(kind).build()
        resp = self._client.im.v1.message_resource.get(req)
        check_response(resp, f"download {kind}")
        data = resp.file.read() if resp.file is not None else b""
        if not data:
            raise FeishuError(None, f"download vuoto per {key}")
        name = name_hint or resp.file_name or key
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(dest_dir, timestamped_name(name))
        dest.write_bytes(data)
        log.info("Feishu resource %s saved to %s (%d bytes)", key, dest, len(data))
        return dest

    async def download_resource(
        self,
        message_id: str,
        key: str,
        kind: Literal["file", "image"],
        dest_dir: Path,
        name_hint: str,
    ) -> Path:
        """Download a message resource into ``dest_dir``; raises :class:`FeishuError`."""
        try:
            return await asyncio.to_thread(self._download_sync, message_id, key, kind, Path(dest_dir), name_hint)
        except FeishuError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Feishu download of %s failed", key)
            raise FeishuError(None, f"download fallito: {exc}") from exc
