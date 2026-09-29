"""Feishu (Lark) integration: REST client, event parsing and long connection."""

from .client import FeishuClient, FeishuError, resolve_domain
from .content import (
    looks_like_markdown,
    markdown_card,
    split_text,
    strip_mentions,
    text_content,
)
from .events import Attachment, IncomingMessage, parse_message_event
from .ws import LongConnection

__all__ = [
    "Attachment",
    "FeishuClient",
    "FeishuError",
    "IncomingMessage",
    "LongConnection",
    "looks_like_markdown",
    "markdown_card",
    "parse_message_event",
    "resolve_domain",
    "split_text",
    "strip_mentions",
    "text_content",
]
