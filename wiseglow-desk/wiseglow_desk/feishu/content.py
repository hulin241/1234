"""Pure helpers for Feishu message content.

Everything here is side-effect free and independent of the lark-oapi SDK:
decoding of incoming ``content`` JSON, mention stripping, chunking of
outgoing text and the Markdown -> interactive card conversion.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Feishu text messages accept up to ~150 KB, but shorter chunks stay readable.
DEFAULT_SPLIT_LIMIT = 3500

_MENTION_RE = re.compile(r"@_(?:user_\d+|all)\b")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_MD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M),  # headings
    re.compile(r"^\s*[-*+]\s+\S", re.M),  # bullet lists
    re.compile(r"^\s*\d+[.)]\s+\S", re.M),  # numbered lists
    re.compile(r"\*\*[^*\n]+\*\*"),  # bold
    re.compile(r"^\s*\|.+\|\s*$", re.M),  # table rows
    re.compile(r"```"),  # code fences
    re.compile(r"\[[^\]\n]+\]\(https?://[^)\s]+\)"),  # links
)
# Paragraphs first, then lines, then words: never split inside a word.
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", " ")

# Italian descriptions for message types the bot cannot process.
_UNSUPPORTED_IT: dict[str, str] = {
    "audio": "[messaggio audio non supportato]",
    "media": "[messaggio video non supportato]",
    "video": "[messaggio video non supportato]",
    "sticker": "[sticker non supportato]",
    "merge_forward": "[messaggi inoltrati non supportati]",
    "share_chat": "[condivisione chat non supportata]",
    "share_user": "[condivisione contatto non supportata]",
    "location": "[posizione non supportata]",
    "interactive": "[scheda interattiva non supportata]",
    "share_calendar_event": "[evento calendario non supportato]",
}


# ---------------------------------------------------------------------------
# Incoming content
# ---------------------------------------------------------------------------


@dataclass
class DecodedContent:
    """Result of decoding a Feishu ``content`` JSON string.

    ``images`` holds image keys, ``files`` holds ``(file_key, file_name)``
    pairs. ``text`` is already cleaned of mention placeholders.
    """

    text: str = ""
    images: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)


def strip_mentions(text: str) -> str:
    """Remove ``@_user_N`` / ``@_all`` placeholders and tidy whitespace."""
    cleaned = _MENTION_RE.sub("", text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines()).strip()


def decode_content(message_type: str, raw: str) -> DecodedContent:
    """Decode the ``content`` of an incoming message by ``message_type``.

    Never raises: malformed JSON degrades to the raw text.
    """
    try:
        data: Any = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return DecodedContent(text=strip_mentions(raw or ""))
    if not isinstance(data, dict):
        return DecodedContent(text=strip_mentions(str(data)))

    if message_type == "text":
        return DecodedContent(text=strip_mentions(str(data.get("text", ""))))
    if message_type == "post":
        return _decode_post(data)
    if message_type == "image":
        key = str(data.get("image_key", "") or "")
        return DecodedContent(images=[key] if key else [])
    if message_type == "file":
        key = str(data.get("file_key", "") or "")
        name = str(data.get("file_name") or f"{key}.bin")
        return DecodedContent(files=[(key, name)] if key else [])
    label = _UNSUPPORTED_IT.get(message_type, f"[messaggio di tipo {message_type or 'sconosciuto'} non supportato]")
    return DecodedContent(text=label)


def _unwrap_post_locale(data: dict[str, Any]) -> dict[str, Any]:
    """Return the post body, unwrapping ``{"zh_cn": {...}}`` style envelopes."""
    if "content" in data:
        return data
    for value in data.values():
        if isinstance(value, dict) and "content" in value:
            return value
    return data


def _post_element_text(el: dict[str, Any], out: DecodedContent) -> str:
    """Render one rich-text element as plain text, collecting attachments."""
    tag = el.get("tag")
    if tag in ("text", "md", "code_block"):
        return str(el.get("text", ""))
    if tag == "a":
        text, href = str(el.get("text", "")), str(el.get("href", ""))
        return f"{text} ({href})" if href and href != text else text or href
    if tag == "at":
        user_id = str(el.get("user_id", ""))
        if user_id.startswith("@_"):
            return user_id
        name = el.get("user_name") or el.get("name")
        return f"@{name}" if name else ""
    if tag == "img":
        key = str(el.get("image_key", "") or "")
        if key:
            out.images.append(key)
        return ""
    if tag == "media":
        key = str(el.get("file_key", "") or "")
        if key:
            out.files.append((key, str(el.get("file_name") or f"{key}.mp4")))
        return ""
    if tag == "hr":
        return "---"
    return ""


def _decode_post(data: dict[str, Any]) -> DecodedContent:
    """Flatten a ``post`` (rich text) message into text lines."""
    body = _unwrap_post_locale(data)
    out = DecodedContent()
    lines: list[str] = []
    title = str(body.get("title") or "").strip()
    if title:
        lines.append(title)
    for paragraph in body.get("content") or []:
        if not isinstance(paragraph, list):
            continue
        parts = [_post_element_text(el, out) for el in paragraph if isinstance(el, dict)]
        lines.append("".join(parts))
    out.text = strip_mentions("\n".join(lines))
    return out


# ---------------------------------------------------------------------------
# Outgoing text
# ---------------------------------------------------------------------------


def split_text(text: str, limit: int = DEFAULT_SPLIT_LIMIT) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    Splits on paragraph boundaries first, then lines, then spaces, so a word
    is never cut in half (a run with no separators at all is hard-cut as a
    last resort). Returns ``[]`` for blank input.
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = max(1, limit)
    return [chunk for chunk in (c.strip() for c in _split(text, limit, 0)) if chunk]


def _split(text: str, limit: int, level: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    if level >= len(_SEPARATORS):
        return [text[i : i + limit] for i in range(0, len(text), limit)]
    sep = _SEPARATORS[level]
    parts = text.split(sep)
    if len(parts) == 1:
        return _split(text, limit, level + 1)

    chunks: list[str] = []
    current = ""
    for part in parts:
        if len(part) > limit:
            if current:
                chunks.append(current)
            sub = _split(part, limit, level + 1)
            chunks.extend(sub[:-1])
            current = sub[-1]
            continue
        candidate = part if not current else f"{current}{sep}{part}"
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    return chunks


def looks_like_markdown(text: str) -> bool:
    """True when the text uses Markdown that a plain text message would mangle."""
    if not text:
        return False
    return any(p.search(text) for p in _MD_PATTERNS)


def _card_markdown(text: str) -> str:
    """Adapt generic Markdown to the Feishu card ``markdown`` element."""
    lines: list[str] = []
    for line in (text or "").splitlines():
        if _FENCE_RE.match(line):
            continue  # code fences become plain text
        heading = _HEADING_RE.match(line)
        if heading:
            label = heading.group(1).replace("**", "").strip()
            lines.append(f"**{label}**" if label else "")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def markdown_card(text: str, title: str | None = None) -> dict[str, Any]:
    """Build a Feishu interactive card (schema 2.0) with one markdown element."""
    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": [{"tag": "markdown", "content": _card_markdown(text)}]},
    }
    if title:
        card["header"] = {"title": {"tag": "plain_text", "content": title}, "template": "blue"}
    return card


def text_content(text: str) -> str:
    """JSON payload for a ``text`` message."""
    return json.dumps({"text": text}, ensure_ascii=False)


def card_content(card: dict[str, Any]) -> str:
    """JSON payload for an ``interactive`` (card) message."""
    return json.dumps(card, ensure_ascii=False)
