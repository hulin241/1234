"""Incoming Feishu message events -> :class:`IncomingMessage`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lark_oapi.api.im.v1 import MentionEvent, P2ImMessageReceiveV1

from .content import decode_content


@dataclass
class Attachment:
    """A downloadable resource attached to a message."""

    kind: Literal["file", "image"]
    key: str
    name: str  # for images: "<image_key>.png"


@dataclass
class IncomingMessage:
    """A normalised view of an ``im.message.receive_v1`` event."""

    message_id: str
    chat_id: str
    chat_type: Literal["p2p", "group"]
    sender_open_id: str
    sender_name: str | None
    text: str  # mention placeholders removed, whitespace trimmed; post: flattened
    mentions_bot: bool
    mentions_others: list[str]  # display names of mentioned users other than the bot
    parent_id: str | None
    root_id: str | None
    attachments: list[Attachment] = field(default_factory=list)
    message_type: str = "text"
    create_time_ms: int = 0
    raw_content: str = ""

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


def _is_bot_mention(mention: MentionEvent, bot_open_id: str | None, bot_name: str | None) -> bool:
    """Decide whether a mention targets our bot.

    With a known ``bot_open_id`` the comparison is exact. Without it we fall
    back to the bot name, and finally to ``mentioned_type == "bot"``.
    """
    open_id = mention.id.open_id if mention.id is not None else None
    if bot_open_id:
        return open_id == bot_open_id
    if bot_name and mention.name:
        return mention.name == bot_name
    return (mention.mentioned_type or "").lower() == "bot"


def _to_int(value: object) -> int:
    """The SDK does not coerce scalars, so timestamps may arrive as strings."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def parse_message_event(
    event: P2ImMessageReceiveV1,
    bot_open_id: str | None,
    bot_name: str | None = None,
) -> IncomingMessage:
    """Convert a raw SDK event into an :class:`IncomingMessage`.

    Raises ``ValueError`` when the event carries no message.
    """
    data = event.event
    message = data.message if data is not None else None
    if message is None or not message.message_id:
        raise ValueError("Evento Feishu senza messaggio")

    sender = data.sender
    sender_open_id = ""
    if sender is not None and sender.sender_id is not None:
        sender_open_id = sender.sender_id.open_id or ""

    message_type = message.message_type or "text"
    raw_content = message.content or ""
    decoded = decode_content(message_type, raw_content)

    mentions = message.mentions or []
    mentions_bot = any(_is_bot_mention(m, bot_open_id, bot_name) for m in mentions)
    mentions_others = [m.name for m in mentions if m.name and not _is_bot_mention(m, bot_open_id, bot_name)]

    attachments = [Attachment(kind="image", key=k, name=f"{k}.png") for k in decoded.images]
    attachments += [Attachment(kind="file", key=k, name=n) for k, n in decoded.files]

    chat_type: Literal["p2p", "group"] = "p2p" if message.chat_type == "p2p" else "group"
    return IncomingMessage(
        message_id=message.message_id,
        chat_id=message.chat_id or "",
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        sender_name=None,  # the event carries no display name
        text=decoded.text,
        mentions_bot=mentions_bot,
        mentions_others=mentions_others,
        parent_id=message.parent_id or None,
        root_id=message.root_id or None,
        attachments=attachments,
        message_type=message_type,
        create_time_ms=_to_int(message.create_time),
        raw_content=raw_content,
    )
