"""Per-chat state: Claude session id, lock, queue and buffered group context.

Everything lives under ``<workspace>/chats/<chat_id>/``:

- ``session.json``  ``{"session_id": "...", "updated": "..."}``
- ``context.jsonl`` group messages that were not addressed to the bot, kept
  as context for the next turn (newest 40 entries).

:class:`SessionRegistry` hands out one :class:`ChatState` per chat and
de-duplicates Feishu message ids (delivery is at-least-once).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..feishu.events import IncomingMessage

log = logging.getLogger(__name__)

MAX_CONTEXT_ENTRIES = 40
DEDUPE_SIZE = 2000

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_chat_dirname(chat_id: str) -> str:
    """A filesystem-safe folder name for a chat id (Feishu ids are already safe)."""
    return _UNSAFE_RE.sub("_", chat_id).strip("._") or "chat"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class ChatState:
    """State of one Feishu chat, persisted under ``workspace/chats/<chat_id>/``."""

    def __init__(self, chat_id: str, root: Path) -> None:
        self.chat_id = chat_id
        self.root = Path(root)
        self.session_id: str | None = None
        self.updated: str | None = None
        self.last_cost_usd: float | None = None  # in-memory only, for /status
        self.lock = asyncio.Lock()
        self.queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()

    # -- paths ---------------------------------------------------------------

    @property
    def session_path(self) -> Path:
        return self.root / "session.json"

    @property
    def context_path(self) -> Path:
        return self.root / "context.jsonl"

    # -- session persistence -------------------------------------------------

    def load(self) -> None:
        """Read ``session.json`` if present; a corrupt file is treated as absent."""
        if not self.session_path.is_file():
            return
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("session.json illeggibile per %s (%s): ignorato", self.chat_id, exc)
            return
        if isinstance(data, dict):
            sid = data.get("session_id")
            self.session_id = sid if isinstance(sid, str) and sid else None
            upd = data.get("updated")
            self.updated = upd if isinstance(upd, str) else None

    def save(self) -> None:
        """Write ``session.json`` atomically."""
        self.updated = _now_iso()
        payload = {"session_id": self.session_id, "updated": self.updated}
        _write_atomic(self.session_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def reset(self) -> None:
        """Forget the Claude session (next turn starts from scratch)."""
        self.session_id = None
        self.save()

    # -- buffered group context ----------------------------------------------

    def append_context(self, msg: IncomingMessage, attachment_paths: list[Path] | None = None) -> None:
        """Append an unaddressed group message to ``context.jsonl`` (newest 40 kept)."""
        entry = {
            "time": _format_time(msg.create_time_ms),
            "sender": msg.sender_name or msg.sender_open_id or "?",
            "text": msg.text,
            "attachments": [str(p) for p in (attachment_paths or [])],
            "message_id": msg.message_id,
        }
        entries = self._read_context()
        entries.append(entry)
        if len(entries) > MAX_CONTEXT_ENTRIES:
            entries = entries[-MAX_CONTEXT_ENTRIES:]
        self._write_context(entries)

    def drain_context(self) -> list[dict[str, Any]]:
        """Return the buffered entries (oldest first) and empty the buffer."""
        entries = self._read_context()
        if self.context_path.exists():
            try:
                self.context_path.unlink()
            except OSError as exc:
                log.warning("Impossibile svuotare il contesto di %s: %s", self.chat_id, exc)
        return entries

    def _read_context(self) -> list[dict[str, Any]]:
        if not self.context_path.is_file():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.context_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                entries.append(item)
        return entries

    def _write_context(self, entries: list[dict[str, Any]]) -> None:
        text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
        _write_atomic(self.context_path, text)


def _format_time(create_time_ms: int) -> str:
    if not create_time_ms:
        return _now_iso()
    try:
        moment = dt.datetime.fromtimestamp(create_time_ms / 1000, tz=dt.timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return _now_iso()
    return moment.strftime("%Y-%m-%d %H:%M")


class SessionRegistry:
    """One :class:`ChatState` per chat id plus message-id de-duplication."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.chats_dir = self.workspace / "chats"
        self._chats: dict[str, ChatState] = {}
        self._seen: deque[str] = deque(maxlen=DEDUPE_SIZE)
        self._seen_set: set[str] = set()

    def get(self, chat_id: str) -> ChatState:
        """The state for ``chat_id`` (loaded from disk on first access)."""
        state = self._chats.get(chat_id)
        if state is None:
            state = ChatState(chat_id, self.chats_dir / safe_chat_dirname(chat_id))
            state.load()
            self._chats[chat_id] = state
        return state

    def seen_message(self, message_id: str) -> bool:
        """True if ``message_id`` was already seen; otherwise record it and return False."""
        if message_id in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(message_id)
        self._seen_set.add(message_id)
        return False

    def __len__(self) -> int:
        return len(self._chats)
