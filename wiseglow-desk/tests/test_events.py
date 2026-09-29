"""Tests for feishu/events.py (real SDK event objects) and feishu/ws.py."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import lark_oapi as lark  # noqa: E402
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: E402
from lark_oapi.core.json import JSON  # noqa: E402

from wiseglow_desk.feishu.events import Attachment, IncomingMessage, parse_message_event  # noqa: E402
from wiseglow_desk.feishu.ws import LongConnection  # noqa: E402

BOT = "ou_bot_0000000000000000000000000000"
USER = "ou_user_111111111111111111111111111"
OTHER = "ou_other_22222222222222222222222222"


def _event(
    message_type: str,
    content: dict[str, Any],
    *,
    chat_type: str = "group",
    mentions: list[dict[str, Any]] | None = None,
    parent_id: str | None = None,
    root_id: str | None = None,
    message_id: str = "om_1234567890abcdef",
) -> P2ImMessageReceiveV1:
    """Build a real SDK event object from a realistic ``im.message.receive_v1`` payload."""
    message: dict[str, Any] = {
        "message_id": message_id,
        "create_time": "1727614800123",  # Feishu sends timestamps as strings
        "update_time": "1727614800123",
        "chat_id": "oc_abc123",
        "chat_type": chat_type,
        "message_type": message_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    if mentions is not None:
        message["mentions"] = mentions
    if parent_id:
        message["parent_id"] = parent_id
    if root_id:
        message["root_id"] = root_id
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "ev_5e3702a84e847582be8db7fb73283c02",
            "event_type": "im.message.receive_v1",
            "create_time": "1727614800200",
            "token": "",
            "app_id": "cli_test",
            "tenant_key": "2ca1d211f64f1234",
        },
        "event": {
            "sender": {
                "sender_id": {"union_id": "on_8ed6aa67826108097d9ee143816345", "user_id": "e33ggbyz", "open_id": USER},
                "sender_type": "user",
                "tenant_key": "2ca1d211f64f1234",
            },
            "message": message,
        },
    }
    return JSON.unmarshal(json.dumps(payload, ensure_ascii=False), P2ImMessageReceiveV1)


def _mention(key: str, open_id: str, name: str, mentioned_type: str = "user") -> dict[str, Any]:
    return {
        "key": key,
        "id": {"union_id": "on_x", "user_id": "u_x", "open_id": open_id},
        "name": name,
        "tenant_key": "2ca1d211f64f1234",
        "mentioned_type": mentioned_type,
    }


def test_sdk_object_shape_is_real() -> None:
    ev = _event("text", {"text": "ciao"})
    assert isinstance(ev, P2ImMessageReceiveV1)
    assert ev.header.event_type == "im.message.receive_v1"
    assert ev.event.message.chat_id == "oc_abc123"
    assert ev.event.sender.sender_id.open_id == USER


def test_text_with_bot_mention_in_group() -> None:
    ev = _event(
        "text",
        {"text": "@_user_1 mi prepari una NBO per il terreno di Lecce? cc @_user_2"},
        mentions=[_mention("@_user_1", BOT, "WiseGlow Desk", "bot"), _mention("@_user_2", OTHER, "Anna Bianchi")],
        parent_id="om_parent",
        root_id="om_root",
    )
    msg = parse_message_event(ev, BOT)
    assert isinstance(msg, IncomingMessage)
    assert msg.message_id == "om_1234567890abcdef"
    assert msg.chat_id == "oc_abc123"
    assert msg.chat_type == "group"
    assert msg.sender_open_id == USER
    assert msg.sender_name is None
    assert msg.text == "mi prepari una NBO per il terreno di Lecce? cc"
    assert msg.mentions_bot is True
    assert msg.mentions_others == ["Anna Bianchi"]
    assert msg.parent_id == "om_parent" and msg.root_id == "om_root"
    assert msg.attachments == []
    assert msg.message_type == "text"
    assert msg.create_time_ms == 1727614800123
    assert json.loads(msg.raw_content)["text"].startswith("@_user_1")


def test_text_mentioning_only_another_user() -> None:
    ev = _event("text", {"text": "@_user_1 hai visto la visura?"}, mentions=[_mention("@_user_1", OTHER, "Anna Bianchi")])
    msg = parse_message_event(ev, BOT)
    assert msg.mentions_bot is False
    assert msg.mentions_others == ["Anna Bianchi"]
    assert msg.text == "hai visto la visura?"


def test_bot_mention_fallbacks_when_open_id_unknown() -> None:
    ev = _event("text", {"text": "@_user_1 ciao"}, mentions=[_mention("@_user_1", BOT, "WiseGlow Desk", "bot")])
    assert parse_message_event(ev, None).mentions_bot is True  # mentioned_type == "bot"
    assert parse_message_event(ev, None, bot_name="WiseGlow Desk").mentions_bot is True
    assert parse_message_event(ev, None, bot_name="Altro Bot").mentions_bot is False
    assert parse_message_event(ev, OTHER).mentions_bot is False  # exact open_id wins


def test_p2p_text_without_mentions() -> None:
    ev = _event("text", {"text": "  ciao, sei lì?  "}, chat_type="p2p")
    msg = parse_message_event(ev, BOT)
    assert msg.chat_type == "p2p"
    assert msg.mentions_bot is False
    assert msg.mentions_others == []
    assert msg.text == "ciao, sei lì?"
    assert msg.parent_id is None and msg.root_id is None


def test_image_message() -> None:
    ev = _event("image", {"image_key": "img_v2_abcdef"})
    msg = parse_message_event(ev, BOT)
    assert msg.message_type == "image"
    assert msg.text == ""
    assert msg.attachments == [Attachment(kind="image", key="img_v2_abcdef", name="img_v2_abcdef.png")]
    assert msg.has_attachments


def test_file_message() -> None:
    ev = _event("file", {"file_key": "file_v2_0a1b", "file_name": "Visura catastale.pdf"})
    msg = parse_message_event(ev, BOT)
    assert msg.attachments == [Attachment(kind="file", key="file_v2_0a1b", name="Visura catastale.pdf")]
    assert msg.text == ""


def test_post_plain_with_mention_and_image() -> None:
    post = {
        "title": "Terreno agricolo Lecce",
        "content": [
            [{"tag": "text", "text": "Ciao "}, {"tag": "at", "user_id": "@_user_1", "user_name": "WiseGlow Desk"}, {"tag": "text", "text": ", ecco i dati:"}],
            [{"tag": "text", "text": "12 ha, "}, {"tag": "a", "text": "visura", "href": "https://example.it/visura.pdf"}],
            [{"tag": "img", "image_key": "img_v2_post"}],
        ],
    }
    ev = _event("post", post, mentions=[_mention("@_user_1", BOT, "WiseGlow Desk", "bot")])
    msg = parse_message_event(ev, BOT)
    assert msg.message_type == "post"
    assert msg.mentions_bot is True
    assert msg.text == "Terreno agricolo Lecce\nCiao , ecco i dati:\n12 ha, visura (https://example.it/visura.pdf)"
    assert msg.attachments == [Attachment(kind="image", key="img_v2_post", name="img_v2_post.png")]


def test_post_locale_wrapped() -> None:
    post = {"zh_cn": {"title": "土地", "content": [[{"tag": "text", "text": "请准备意向书"}], [{"tag": "img", "image_key": "img_cn"}]]}}
    msg = parse_message_event(_event("post", post), BOT)
    assert msg.text == "土地\n请准备意向书"
    assert [a.key for a in msg.attachments] == ["img_cn"]


def test_audio_message_is_described_in_italian() -> None:
    msg = parse_message_event(_event("audio", {"file_key": "file_audio", "duration": 2000}), BOT)
    assert msg.text == "[messaggio audio non supportato]"
    assert msg.attachments == []


def test_event_without_message_raises() -> None:
    ev = JSON.unmarshal(json.dumps({"schema": "2.0", "header": {"event_type": "im.message.receive_v1"}, "event": {}}), P2ImMessageReceiveV1)
    with pytest.raises(ValueError):
        parse_message_event(ev, BOT)


# ---------------------------------------------------------------------------
# LongConnection
# ---------------------------------------------------------------------------


class _FakeWsClient:
    """Mimics ``lark.ws.Client``: ``start()`` returns or raises immediately."""

    instances: list["_FakeWsClient"] = []

    def __init__(self, app_id: str, app_secret: str, *, event_handler: Any, log_level: Any, domain: str) -> None:
        self.app_id, self.event_handler, self.domain = app_id, event_handler, domain
        self.started = threading.Event()
        _FakeWsClient.instances.append(self)

    def start(self) -> None:
        self.started.set()
        if len(_FakeWsClient.instances) % 2 == 1:
            raise RuntimeError("simulated connect failure")


def test_long_connection_restarts_and_stops() -> None:
    _FakeWsClient.instances.clear()
    received: list[Any] = []
    conn = LongConnection("cli_x", "secret", "lark", received.append, client_factory=_FakeWsClient)
    conn.restart_delay = 0.02
    thread = conn.start_in_thread()
    assert thread.daemon and thread.name == "feishu-ws"
    assert conn.start_in_thread() is thread  # idempotent while alive
    deadline = time.time() + 2
    while conn.restarts < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert conn.restarts >= 2  # restarted after both a raise and a plain return
    conn.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert conn.stopped
    first = _FakeWsClient.instances[0]
    assert first.domain == lark.LARK_DOMAIN and first.app_id == "cli_x"
    assert isinstance(first.event_handler, lark.EventDispatcherHandler)


def test_long_connection_handler_dispatches_real_payload_and_swallows_errors() -> None:
    received: list[P2ImMessageReceiveV1] = []

    def on_event(ev: P2ImMessageReceiveV1) -> None:
        received.append(ev)
        raise RuntimeError("bot layer bug")  # must not propagate to the SDK

    conn = LongConnection("cli_x", "secret", "feishu", on_event)
    handler = conn.build_handler()
    payload = {
        "schema": "2.0",
        "header": {"event_id": "ev_1", "event_type": "im.message.receive_v1", "create_time": "1", "token": "", "app_id": "cli_x", "tenant_key": "t"},
        "event": {"sender": {"sender_id": {"open_id": USER}, "sender_type": "user"}, "message": {"message_id": "om_x", "chat_id": "oc_1", "chat_type": "p2p", "message_type": "text", "content": "{\"text\":\"ciao\"}"}},
    }
    handler._do_without_validation(json.dumps(payload).encode("utf-8"))  # what the WS client calls
    assert len(received) == 1
    assert parse_message_event(received[0], BOT).text == "ciao"


class _BlockingWsClient:
    """Mimics the real SDK after a successful connect: ``start()`` sleeps forever in the SDK loop.

    ``_conn`` mirrors ``lark.ws.Client._conn`` (None = no WebSocket connection).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._conn: object | None = None
        self.started = threading.Event()

    def start(self) -> None:
        self.started.set()
        self._loop.run_until_complete(self._select())

    @staticmethod
    async def _select() -> None:
        while True:
            await asyncio.sleep(3600)


def _wait_until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_long_connection_watchdog_restarts_a_dead_connection_and_stop_unblocks() -> None:
    sdk_loop = asyncio.new_event_loop()
    clients: list[_BlockingWsClient] = []

    def factory(*args: Any, **kwargs: Any) -> _BlockingWsClient:
        client = _BlockingWsClient(sdk_loop)
        clients.append(client)
        return client

    conn = LongConnection("cli_x", "secret", "feishu", lambda e: None, client_factory=factory, sdk_loop=sdk_loop)
    conn.restart_delay = 0.02
    conn.watchdog_interval = 0.02
    conn.dead_after = 0.1
    thread = conn.start_in_thread()
    try:
        # 1) the first client never gets a connection: the watchdog cancels the SDK loop and we restart
        assert _wait_until(lambda: conn.restarts >= 1)
        assert conn.watchdog_kicks >= 1
        assert thread.is_alive()

        # 2) the second client is connected: the watchdog stays quiet
        assert _wait_until(lambda: len(clients) >= 2 and clients[-1].started.is_set())
        clients[-1]._conn = object()
        restarts = conn.restarts
        time.sleep(0.3)  # > dead_after + interval
        assert conn.restarts == restarts

        # 3) stop() unblocks start() and the thread exits
        conn.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert conn.stopped
    finally:
        conn.stop()
        thread.join(timeout=2)
        sdk_loop.close()


def test_long_connection_uses_the_sdk_loop_by_default() -> None:
    from lark_oapi.ws import client as sdk_ws

    conn = LongConnection("cli_x", "secret", "feishu", lambda e: None)
    assert conn.sdk_loop is sdk_ws.loop
    conn.stop()  # loop not running: nothing to cancel, must not raise
