"""Tests for wiseglow_desk.bot (Desk wiring), config.py, log.py and run.py.

Everything runs with fakes: no Feishu, no Claude, no network.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wiseglow_desk.agent.brain import TurnResult  # noqa: E402
from wiseglow_desk.agent.sessions import SessionRegistry  # noqa: E402
from wiseglow_desk.bot import (  # noqa: E402
    HELP_TEXT,
    Check,
    Desk,
    check_anthropic_key,
    check_brand,
    checks_ok,
    ensure_workspace,
    format_report,
    parse_command,
    render_sample,
    run_checks,
)
from wiseglow_desk.config import (  # noqa: E402
    PROJECT_ROOT,
    ConfigError,
    Settings,
    describe,
    load_settings,
    settings_from_env,
)
from wiseglow_desk.docgen.brand import Brand  # noqa: E402
from wiseglow_desk.feishu.events import Attachment, IncomingMessage  # noqa: E402
from wiseglow_desk.log import setup_logging  # noqa: E402

BRAND_PATH = ROOT / "brand" / "brand.yaml"
_ids = itertools.count(1)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeFeishu:
    """Records every call; downloads write a small file into dest_dir."""

    def __init__(self, open_id: str | None = "ou_bot") -> None:
        self.sent_message_ids: deque[str] = deque(maxlen=500)
        self.bot_name: str | None = "WiseGlow Desk"
        self._open_id = open_id
        self.sent: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []
        self.downloads: list[Path] = []
        self.fail_downloads = False
        self.download_delay = 0.0
        self.identity_calls = 0
        self._counter = 0

    async def bot_open_id(self) -> str | None:
        self.identity_calls += 1
        return self._open_id

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        self._counter += 1
        message_id = f"om_bot_{self._counter}"
        self.sent.append((chat_id, text, reply_to))
        self.sent_message_ids.append(message_id)
        return message_id

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str | None:
        self.reactions.append((message_id, emoji_type))
        return f"reaction-{len(self.reactions)}"

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        self.removed.append((message_id, reaction_id))

    async def download_resource(self, message_id: str, key: str, kind: str, dest_dir: Path, name_hint: str) -> Path:
        if self.fail_downloads:
            raise RuntimeError("rete assente")
        if self.download_delay:
            await asyncio.sleep(self.download_delay)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{key}_{name_hint}"
        path.write_bytes(b"contenuto")
        self.downloads.append(path)
        return path

    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


class FakeBrain:
    """Returns a scripted TurnResult (or raises) and records the call."""

    def __init__(self, result: TurnResult | None = None, raise_exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or TurnResult(text="Fatto!", session_id="sess-1", cost_usd=0.12, error=None, duration_ms=10)
        self.raise_exc = raise_exc
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None  # when set, run_turn waits for it

    async def run_turn(self, chat: Any, msg: IncomingMessage, attachment_paths: list[Path], context: list[dict]) -> TurnResult:
        self.calls.append({"chat": chat, "msg": msg, "paths": list(attachment_paths), "context": list(context)})
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.raise_exc is not None:
            raise self.raise_exc
        chat.session_id = self.result.session_id
        chat.save()
        chat.last_cost_usd = self.result.cost_usd
        return self.result


class FakeStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def list(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.rows if chat_id is None or r.get("chat_id") == chat_id]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        workspace_dir=tmp_path / "workspace",
        brand_path=BRAND_PATH,
        anthropic_api_key="sk-ant-test",
    )
    base.update(overrides)
    return Settings(**base)


def make_msg(**overrides: Any) -> IncomingMessage:
    base: dict[str, Any] = dict(
        message_id=f"om_user_{next(_ids)}",
        chat_id="oc_group",
        chat_type="group",
        sender_open_id="ou_user",
        sender_name=None,
        text="ciao, mi prepari una NBO?",
        mentions_bot=False,
        mentions_others=[],
        parent_id=None,
        root_id=None,
        attachments=[],
        message_type="text",
        create_time_ms=1_700_000_000_000,
        raw_content="",
    )
    base.update(overrides)
    return IncomingMessage(**base)


def make_desk(tmp_path: Path, brain: FakeBrain | None = None, **settings_overrides: Any) -> tuple[Desk, FakeFeishu, FakeBrain]:
    settings = make_settings(tmp_path, **settings_overrides)
    feishu = FakeFeishu()
    brain = brain or FakeBrain()
    desk = Desk(
        settings,
        feishu=feishu,
        brain=brain,
        registry=SessionRegistry(settings.workspace_dir),
        store=FakeStore(),
        brand=Brand(),
    )
    return desk, feishu, brain


# ---------------------------------------------------------------------------
# addressed decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"chat_type": "p2p"}, True),
        ({"mentions_bot": True}, True),
        ({"parent_id": "om_bot_known"}, True),
        ({"parent_id": "om_someone_else"}, False),
        ({}, False),
    ],
)
def test_is_addressed_in_mention_mode(tmp_path: Path, overrides: dict, expected: bool) -> None:
    desk, feishu, _ = make_desk(tmp_path)
    feishu.sent_message_ids.append("om_bot_known")
    assert desk.is_addressed(make_msg(**overrides)) is expected


def test_trigger_mode_all_addresses_every_group_message(tmp_path: Path) -> None:
    desk, _, _ = make_desk(tmp_path, trigger_mode="all")
    assert desk.is_addressed(make_msg()) is True


# ---------------------------------------------------------------------------
# routing: dedupe, chat filter, context buffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_message_is_ignored(tmp_path: Path) -> None:
    desk, _, brain = make_desk(tmp_path)
    msg = make_msg(chat_type="p2p")
    assert await desk.handle_message(msg) == "queued"
    assert await desk.handle_message(msg) == "duplicate"
    await desk.drain()
    assert len(brain.calls) == 1


@pytest.mark.asyncio
async def test_chat_not_in_allowlist_is_ignored(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path, allowed_chat_ids=("oc_allowed",))
    assert await desk.handle_message(make_msg(chat_type="p2p", chat_id="oc_other")) == "chat_not_allowed"
    allowed = make_msg(chat_type="p2p", chat_id="oc_allowed")
    assert await desk.handle_message(allowed) == "queued"
    await desk.drain()
    assert len(brain.calls) == 1
    assert feishu.reactions and all(mid == allowed.message_id for mid, _ in feishu.reactions)


@pytest.mark.asyncio
async def test_unaddressed_group_message_is_buffered_quietly(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    msg = make_msg(text="il terreno è a Lecce, 12 ettari")
    assert await desk.handle_message(msg) == "context"
    await desk.drain()  # buffering happens in the per-chat worker
    chat = desk.registry.get(msg.chat_id)
    assert chat.context_path.is_file()
    assert "Lecce" in chat.context_path.read_text(encoding="utf-8")
    assert brain.calls == []
    assert feishu.reactions == []
    assert feishu.sent == []


@pytest.mark.asyncio
async def test_buffered_context_is_passed_to_next_turn_and_cleared(tmp_path: Path) -> None:
    desk, _, brain = make_desk(tmp_path)
    await desk.handle_message(make_msg(text="prezzo 1,2 milioni"))
    await desk.handle_message(make_msg(mentions_bot=True, text="fammi la NBO"))
    await desk.drain()
    assert len(brain.calls) == 1
    context = brain.calls[0]["context"]
    assert [c["text"] for c in context] == ["prezzo 1,2 milioni"]
    assert not desk.registry.get("oc_group").context_path.exists()


# ---------------------------------------------------------------------------
# addressed turn: reactions, reply, error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_addressed_message_runs_turn_and_replies(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    msg = make_msg(chat_type="p2p", chat_id="oc_p2p")
    assert await desk.handle_message(msg) == "queued"
    await desk.drain()

    assert len(brain.calls) == 1
    assert brain.calls[0]["msg"] is msg
    assert feishu.sent == [("oc_p2p", "Fatto!", msg.message_id)]
    assert feishu.reactions == [(msg.message_id, "OnIt"), (msg.message_id, "DONE")]
    assert feishu.removed == [(msg.message_id, "reaction-1")]
    assert desk.registry.get("oc_p2p").session_id == "sess-1"


@pytest.mark.asyncio
async def test_error_turn_sends_apology_and_error_reaction(tmp_path: Path) -> None:
    failing = FakeBrain(TurnResult(text="", session_id=None, cost_usd=None, error="budget superato", duration_ms=5))
    desk, feishu, _ = make_desk(tmp_path, brain=failing)
    msg = make_msg(chat_type="p2p")
    await desk.handle_message(msg)
    await desk.drain()

    assert len(feishu.sent) == 1
    assert feishu.sent[0][1].startswith("Scusa")
    assert "budget superato" in feishu.sent[0][1]
    assert feishu.reactions[-1] == (msg.message_id, "ERROR")


@pytest.mark.asyncio
async def test_brain_exception_is_handled(tmp_path: Path) -> None:
    desk, feishu, _ = make_desk(tmp_path, brain=FakeBrain(raise_exc=RuntimeError("boom")))
    msg = make_msg(chat_type="p2p")
    await desk.handle_message(msg)
    await desk.drain()
    assert any("boom" in t for t in feishu.texts())
    assert feishu.reactions[-1] == (msg.message_id, "ERROR")


@pytest.mark.asyncio
async def test_final_text_equal_to_interim_is_not_resent(tmp_path: Path) -> None:
    result = TurnResult(text="Ecco la NBO", session_id="s", cost_usd=1.0, error=None, duration_ms=1,
                        last_interim_text="Ecco la NBO", sent_files=[Path("x.pdf")])
    desk, feishu, _ = make_desk(tmp_path, brain=FakeBrain(result))
    await desk.handle_message(make_msg(chat_type="p2p"))
    await desk.drain()
    assert feishu.sent == []


@pytest.mark.asyncio
async def test_silent_turn_without_files_gets_a_fallback_line(tmp_path: Path) -> None:
    result = TurnResult(text="", session_id="s", cost_usd=None, error=None, duration_ms=1)
    desk, feishu, _ = make_desk(tmp_path, brain=FakeBrain(result))
    await desk.handle_message(make_msg(chat_type="p2p"))
    await desk.drain()
    assert len(feishu.sent) == 1
    assert "riprovo" in feishu.sent[0][1]


@pytest.mark.asyncio
async def test_turns_in_same_chat_are_serialised(tmp_path: Path) -> None:
    brain = FakeBrain()
    brain.release = asyncio.Event()
    desk, _, _ = make_desk(tmp_path, brain=brain)
    first = make_msg(chat_type="p2p")
    second = make_msg(chat_type="p2p")
    await desk.handle_message(first)
    await desk.handle_message(second)
    await asyncio.wait_for(brain.started.wait(), 2)
    await asyncio.sleep(0.05)
    assert len(brain.calls) == 1  # second waits for the first turn
    brain.release.set()
    await asyncio.wait_for(desk.drain(), 2)
    assert [c["msg"].message_id for c in brain.calls] == [first.message_id, second.message_id]


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachments_are_downloaded_even_when_unaddressed(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    msg = make_msg(text="", attachments=[Attachment(kind="file", key="fk1", name="visura.pdf")])
    assert await desk.handle_message(msg) == "context"
    await desk.drain()  # the download happens in the per-chat worker
    assert len(feishu.downloads) == 1
    assert feishu.downloads[0].parent == desk.workspace / "inbox" / "oc_group"
    chat = desk.registry.get("oc_group")
    assert str(feishu.downloads[0]) in chat.context_path.read_text(encoding="utf-8")
    assert brain.calls == []


@pytest.mark.asyncio
async def test_attachment_paths_reach_the_brain(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    msg = make_msg(
        chat_type="p2p",
        attachments=[Attachment(kind="file", key="fk1", name="nbo_vecchia.docx"), Attachment(kind="image", key="img1", name="img1.png")],
    )
    await desk.handle_message(msg)
    await desk.drain()
    assert brain.calls[0]["paths"] == feishu.downloads
    assert len(feishu.downloads) == 2


@pytest.mark.asyncio
async def test_download_failure_notifies_user_when_addressed(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    feishu.fail_downloads = True
    msg = make_msg(chat_type="p2p", attachments=[Attachment(kind="file", key="fk1", name="visura.pdf")])
    await desk.handle_message(msg)
    await desk.drain()
    assert any("visura.pdf" in t for t in feishu.texts())
    assert brain.calls[0]["paths"] == []


@pytest.mark.asyncio
async def test_slow_download_in_one_chat_does_not_stall_other_chats(tmp_path: Path) -> None:
    """The dispatcher only enqueues: a 0.5 s download in chat A must not delay /help in chat B."""
    desk, feishu, brain = make_desk(tmp_path)
    feishu.download_delay = 0.5
    slow = make_msg(chat_id="oc_a", text="", attachments=[Attachment(kind="file", key="fk1", name="grande.pdf")])
    help_msg = make_msg(chat_id="oc_b", chat_type="p2p", text="/help")
    started = asyncio.get_running_loop().time()
    assert await desk.handle_message(slow) == "context"
    assert await desk.handle_message(help_msg) == "command"
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.2, f"/help attese il download: {elapsed:.2f}s"
    assert feishu.sent == [("oc_b", HELP_TEXT, help_msg.message_id)]
    assert feishu.downloads == []  # still downloading
    await desk.drain()
    assert len(feishu.downloads) == 1
    assert "grande.pdf" in desk.registry.get("oc_a").context_path.read_text(encoding="utf-8")
    assert brain.calls == []


@pytest.mark.asyncio
async def test_unaddressed_and_addressed_messages_keep_their_order_within_a_chat(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    feishu.download_delay = 0.05
    await desk.handle_message(make_msg(text="", attachments=[Attachment(kind="file", key="fk1", name="visura.pdf")]))
    await desk.handle_message(make_msg(mentions_bot=True, text="usa la visura che ho mandato"))
    await desk.drain()
    assert len(brain.calls) == 1
    context = brain.calls[0]["context"]
    assert len(context) == 1 and context[0]["attachments"] == [str(feishu.downloads[0])]


# ---------------------------------------------------------------------------
# turn timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hung_turn_is_interrupted_and_frees_the_semaphore(tmp_path: Path) -> None:
    hung = FakeBrain()
    hung.release = asyncio.Event()  # never set: the turn would block forever
    desk, feishu, _ = make_desk(tmp_path, brain=hung, turn_timeout_s=0.2, max_parallel_chats=1)
    first = make_msg(chat_type="p2p", chat_id="oc_a")
    await desk.handle_message(first)
    await asyncio.wait_for(hung.started.wait(), 2)

    # a second chat queues behind the single semaphore slot
    ok = FakeBrain()
    desk.brain = ok
    second = make_msg(chat_type="p2p", chat_id="oc_b")
    await desk.handle_message(second)
    await asyncio.wait_for(desk.drain(), 5)

    apology = [t for c, t, _ in feishu.sent if c == "oc_a"]
    assert len(apology) == 1 and "interrotto" in apology[0] and "0s" in apology[0]
    assert (first.message_id, "ERROR") in feishu.reactions
    assert [c["msg"].message_id for c in ok.calls] == [second.message_id]  # the slot was freed
    assert (second.message_id, "DONE") in feishu.reactions


@pytest.mark.asyncio
async def test_turn_within_timeout_is_untouched(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path, turn_timeout_s=5)
    msg = make_msg(chat_type="p2p")
    await desk.handle_message(msg)
    await desk.drain()
    assert feishu.sent == [("oc_group", "Fatto!", msg.message_id)]
    assert len(brain.calls) == 1


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("/reset", "/reset"), ("  /STATUS ", "/status"), ("/help please", "/help"), ("/unknown", None), ("ciao /reset", None), ("", None)],
)
def test_parse_command(text: str, expected: str | None) -> None:
    assert parse_command(text) == expected


@pytest.mark.asyncio
async def test_reset_command_clears_session_and_context(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    chat = desk.registry.get("oc_group")
    chat.session_id = "old-session"
    chat.save()
    await desk.handle_message(make_msg(text="nota di contesto"))
    msg = make_msg(mentions_bot=True, text="/reset")
    assert await desk.handle_message(msg) == "queued"
    await desk.drain()

    assert chat.session_id is None
    assert not chat.context_path.exists()
    assert brain.calls == []
    assert feishu.sent[-1][2] == msg.message_id
    assert "azzerato" in feishu.sent[-1][1]
    assert feishu.reactions == []


@pytest.mark.asyncio
async def test_status_command_reports_session_documents_and_cost(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    desk.store = FakeStore([{"document_id": "nbo-lecce", "title": "NBO Lecce", "chat_id": "oc_group", "versions": 2}])
    chat = desk.registry.get("oc_group")
    chat.session_id = "sess-xyz"
    chat.last_cost_usd = 0.42
    msg = make_msg(mentions_bot=True, text="/status")
    assert await desk.handle_message(msg) == "command"

    assert brain.calls == []
    text = feishu.sent[-1][1]
    assert "sess-xyz" in text
    assert "Documenti creati in questa chat: 1" in text
    assert "0.42 USD" in text
    assert "NBO Lecce (v2)" in text
    assert "claude-fable-5-1" in text


@pytest.mark.asyncio
async def test_help_command_replies_locally(tmp_path: Path) -> None:
    desk, feishu, brain = make_desk(tmp_path)
    msg = make_msg(chat_type="p2p", text="/help")
    assert await desk.handle_message(msg) == "command"
    assert feishu.sent == [("oc_group", HELP_TEXT, msg.message_id)]
    assert brain.calls == []


@pytest.mark.asyncio
async def test_command_without_mention_in_group_is_only_context(tmp_path: Path) -> None:
    desk, feishu, _ = make_desk(tmp_path)
    assert await desk.handle_message(make_msg(text="/help")) == "context"
    assert feishu.sent == []


# ---------------------------------------------------------------------------
# thread → loop hand-off and lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_event_is_forwarded_from_another_thread(tmp_path: Path) -> None:
    desk, _, _ = make_desk(tmp_path)
    desk._loop = asyncio.get_running_loop()
    event = object()
    thread = threading.Thread(target=desk.on_ws_event, args=(event,))
    thread.start()
    thread.join()
    received = await asyncio.wait_for(desk._events.get(), 2)
    assert received is event


def test_ws_event_before_start_is_dropped(tmp_path: Path) -> None:
    desk, _, _ = make_desk(tmp_path)
    desk.on_ws_event(object())  # must not raise
    assert desk._events.qsize() == 0


class FakeConnection:
    def __init__(self, app_id: str, app_secret: str, domain: str, on_event: Any) -> None:
        self.on_event = on_event
        self.started = False
        self.stopped = False

    def start_in_thread(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_run_starts_connection_and_stops_gracefully(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    feishu, brain = FakeFeishu(), FakeBrain()
    connections: list[FakeConnection] = []

    def factory(*args: Any) -> FakeConnection:
        conn = FakeConnection(*args)
        connections.append(conn)
        return conn

    desk = Desk(settings, feishu=feishu, brain=brain, registry=SessionRegistry(settings.workspace_dir),
                store=FakeStore(), brand=Brand(), connection_factory=factory)
    runner = asyncio.create_task(desk.run())
    await asyncio.sleep(0.05)
    assert connections and connections[0].started
    assert desk.bot_open_id == "ou_bot"
    assert desk._identity_task is None  # resolved at once: no retry loop needed
    desk.request_stop()
    await asyncio.wait_for(runner, 5)
    assert connections[0].stopped


@pytest.mark.asyncio
async def test_identity_is_retried_in_background_until_feishu_answers(tmp_path: Path) -> None:
    """Booting before the network is up must not leave mention detection degraded forever."""
    settings = make_settings(tmp_path)
    feishu = FakeFeishu(open_id=None)  # bot/v3/info fails at boot
    desk = Desk(settings, feishu=feishu, brain=FakeBrain(), registry=SessionRegistry(settings.workspace_dir),
                store=FakeStore(), brand=Brand(), connection_factory=lambda *a: FakeConnection(*a))
    desk.identity_retry_s = 0.02
    runner = asyncio.create_task(desk.run())
    await asyncio.sleep(0.1)
    assert desk.bot_open_id is None and feishu.identity_calls >= 2  # keeps trying, bot stays up
    assert desk._identity_task is not None and not desk._identity_task.done()

    feishu._open_id = "ou_bot"  # the network comes up
    for _ in range(50):
        await asyncio.sleep(0.02)
        if desk.bot_open_id is not None:
            break
    assert desk.bot_open_id == "ou_bot"
    await asyncio.sleep(0.05)
    assert desk._identity_task.done()  # the loop ends once resolved
    calls_after = feishu.identity_calls
    await asyncio.sleep(0.05)
    assert feishu.identity_calls == calls_after  # no more polling
    desk.request_stop()
    await asyncio.wait_for(runner, 5)


@pytest.mark.asyncio
async def test_shutdown_cancels_a_pending_identity_loop(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    desk = Desk(settings, feishu=FakeFeishu(open_id=None), brain=FakeBrain(), registry=SessionRegistry(settings.workspace_dir),
                store=FakeStore(), brand=Brand(), connection_factory=lambda *a: FakeConnection(*a))
    runner = asyncio.create_task(desk.run())
    await asyncio.sleep(0.05)
    assert desk._identity_task is not None
    desk.request_stop()
    await asyncio.wait_for(runner, 5)
    assert desk._identity_task.cancelled()


def test_ensure_workspace_creates_layout_and_notes(tmp_path: Path) -> None:
    ws = ensure_workspace(tmp_path / "ws")
    for sub in ("chats", "inbox", "documents", "knowledge/templates", "logs"):
        assert (ws / sub).is_dir()
    notes = ws / "knowledge" / "NOTES.md"
    assert notes.read_text(encoding="utf-8").startswith("# Note di lavoro")
    notes.write_text("# mie note", encoding="utf-8")
    ensure_workspace(ws)
    assert notes.read_text(encoding="utf-8") == "# mie note"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_settings_from_env_defaults() -> None:
    s = settings_from_env({"FEISHU_APP_ID": "cli_1", "FEISHU_APP_SECRET": "sec"})
    assert s.feishu_domain == "feishu"
    assert s.model == "claude-fable-5-1"
    assert s.effort == "high"
    assert s.trigger_mode == "mention"
    assert s.workspace_dir == (PROJECT_ROOT / "workspace").resolve()
    assert s.brand_path == (PROJECT_ROOT / "brand" / "brand.yaml").resolve()
    assert s.allowed_chat_ids == ()
    assert s.max_parallel_chats == 2 and s.max_turns == 60 and s.max_budget_usd_per_turn == 5.0
    assert s.turn_timeout_s == 900.0
    assert s.anthropic_api_key is None


def test_settings_from_env_parses_everything(tmp_path: Path) -> None:
    env = {
        "FEISHU_APP_ID": "cli_1",
        "FEISHU_APP_SECRET": "sec",
        "FEISHU_DOMAIN": "Lark",
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "WISEGLOW_MODEL": "claude-fable-5-1",
        "WISEGLOW_EFFORT": "xhigh",
        "WISEGLOW_WORKSPACE": str(tmp_path / "ws"),
        "WISEGLOW_BRAND": "brand/altro.yaml",
        "WISEGLOW_TRIGGER_MODE": "all",
        "WISEGLOW_ALLOW_BASH": "yes",
        "WISEGLOW_MAX_BUDGET_USD": "2.5",
        "WISEGLOW_MAX_TURNS": "30",
        "WISEGLOW_MAX_PARALLEL_CHATS": "4",
        "WISEGLOW_TURN_TIMEOUT": "300",
        "WISEGLOW_ALLOWED_CHATS": "oc_a, oc_b,,",
        "WISEGLOW_LOG_LEVEL": "debug",
    }
    s = settings_from_env(env)
    assert s.feishu_domain == "lark"
    assert s.anthropic_api_key == "sk-ant-x" and s.has_anthropic_key
    assert s.effort == "xhigh" and s.trigger_mode == "all" and s.allow_bash is True
    assert s.workspace_dir == (tmp_path / "ws").resolve()
    assert s.brand_path == (PROJECT_ROOT / "brand" / "altro.yaml").resolve()
    assert s.max_budget_usd_per_turn == 2.5 and s.max_turns == 30 and s.max_parallel_chats == 4
    assert s.turn_timeout_s == 300.0
    assert s.allowed_chat_ids == ("oc_a", "oc_b")
    assert s.log_level == "DEBUG"
    assert s.chat_allowed("oc_a") and not s.chat_allowed("oc_z")


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"FEISHU_APP_SECRET": "s"}, "FEISHU_APP_ID"),
        ({"FEISHU_APP_ID": "a"}, "FEISHU_APP_SECRET"),
        ({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "WISEGLOW_EFFORT": "turbo"}, "WISEGLOW_EFFORT"),
        ({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "WISEGLOW_TRIGGER_MODE": "sometimes"}, "WISEGLOW_TRIGGER_MODE"),
        ({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "FEISHU_DOMAIN": "dingtalk"}, "FEISHU_DOMAIN"),
        ({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "WISEGLOW_MAX_TURNS": "molti"}, "WISEGLOW_MAX_TURNS"),
        ({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "WISEGLOW_MAX_PARALLEL_CHATS": "0"}, "WISEGLOW_MAX_PARALLEL_CHATS"),
        ({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "WISEGLOW_ALLOW_BASH": "forse"}, "WISEGLOW_ALLOW_BASH"),
        ({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s", "WISEGLOW_TURN_TIMEOUT": "5"}, "WISEGLOW_TURN_TIMEOUT"),
    ],
)
def test_settings_validation_errors_name_the_variable(env: dict, fragment: str) -> None:
    with pytest.raises(ConfigError) as info:
        settings_from_env(env)
    assert fragment in str(info.value)
    assert isinstance(info.value, ValueError)


def test_settings_without_feishu_for_offline_commands() -> None:
    s = settings_from_env({}, require_feishu=False)
    assert s.feishu_app_id == ""


def test_load_settings_reads_env_file_without_overriding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("FEISHU_", "WISEGLOW_", "ANTHROPIC_"))}
    clean["WISEGLOW_EFFORT"] = "low"
    monkeypatch.setattr(os, "environ", clean)
    env_file = tmp_path / ".env"
    env_file.write_text("FEISHU_APP_ID=cli_file\nFEISHU_APP_SECRET=sec_file\nWISEGLOW_EFFORT=max\n", encoding="utf-8")
    s = load_settings(env_file)
    assert s.feishu_app_id == "cli_file"
    assert s.effort == "low"  # the process environment wins over .env
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "missing.env")


def test_describe_masks_secrets(tmp_path: Path) -> None:
    text = describe(make_settings(tmp_path))
    assert ": secret" not in text and "sk-ant-test" not in text
    assert "feishu_app_secret: impostata" in text
    assert "anthropic_api_key: impostata" in text


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def test_setup_logging_creates_file_and_is_idempotent(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = setup_logging(tmp_path / "ws", "DEBUG")
        again = setup_logging(tmp_path / "ws", "INFO")
        assert path == again == tmp_path / "ws" / "logs" / "desk.log"
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 2
        logging.getLogger("wiseglow.test").warning("riga di prova")
        for h in added:
            h.flush()
        assert "riga di prova" in path.read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


# ---------------------------------------------------------------------------
# --check and --render
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_checks_with_fake_feishu(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    checks = await run_checks(settings, feishu=FakeFeishu())
    by_name = {c.name: c for c in checks}
    assert by_name["Feishu"].status == "ok" and "ou_bot" in by_name["Feishu"].detail
    assert by_name["ANTHROPIC_API_KEY"].status == "ok"
    assert by_name["Brand"].status == "ok"
    assert by_name["Workspace"].status == "ok"
    assert by_name["Python"].status == "ok"
    assert checks_ok(checks)
    report = format_report(checks)
    assert "Controllo WiseGlow Desk" in report and "Tutto pronto" in report


@pytest.mark.asyncio
async def test_run_checks_reports_failures(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, anthropic_api_key=None, brand_path=tmp_path / "nope.yaml")
    checks = await run_checks(settings, feishu=FakeFeishu(open_id=None))
    by_name = {c.name: c for c in checks}
    assert by_name["Feishu"].status == "fail"
    assert by_name["Brand"].status == "warn"
    assert not checks_ok(checks)
    assert "problemi" in format_report(checks)


def test_check_anthropic_key_and_invalid_brand(tmp_path: Path) -> None:
    assert check_anthropic_key(make_settings(tmp_path, anthropic_api_key=None)).status == "fail"
    bad = tmp_path / "brand.yaml"
    bad.write_text("colors:\n  primary: verde\n", encoding="utf-8")
    assert check_brand(make_settings(tmp_path, brand_path=bad)).status == "fail"
    assert isinstance(Check("x", "ok", "y"), Check)


def test_render_sample_produces_docx(tmp_path: Path) -> None:
    result = render_sample(ROOT / "samples" / "nbo.json", BRAND_PATH, tmp_path / "out", with_pdf=False)
    assert result.docx_path.is_file() and result.docx_path.suffix == ".docx"
    assert result.pdf_path is None and result.pages is None


def test_run_py_help_and_render(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import run

    with pytest.raises(SystemExit) as info:
        run.main(["--help"])
    assert info.value.code == 0
    assert "--check" in capsys.readouterr().out

    code = run.main(["--render", str(ROOT / "samples" / "loi.json"), "--out", str(tmp_path / "r"), "--no-pdf"])
    assert code == 0
    out = capsys.readouterr().out
    assert "DOCX:" in out and (tmp_path / "r").is_dir()


def test_check_soffice_reports_invalid_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from wiseglow_desk.bot import check_soffice

    monkeypatch.setenv("WISEGLOW_SOFFICE", str(tmp_path / "LibreOffice.app"))
    detail = check_soffice().detail
    assert "WISEGLOW_SOFFICE=" in detail and "ignorato" in detail
    monkeypatch.delenv("WISEGLOW_SOFFICE")
    assert "WISEGLOW_SOFFICE" not in check_soffice().detail


# ---------------------------------------------------------------------------
# ops review fixes: Lark logger routing, broken brand.yaml, .env.example, install-mac.sh
# ---------------------------------------------------------------------------


def test_setup_logging_routes_lark_logger_through_root_and_quiets_it(tmp_path: Path) -> None:
    import lark_oapi  # noqa: F401  registers the "Lark" logger with its own stdout handler

    root = logging.getLogger()
    before = list(root.handlers)
    lark_logger = logging.getLogger("Lark")
    saved_handlers, saved_level = list(lark_logger.handlers), lark_logger.level
    try:
        path = setup_logging(tmp_path / "ws", "INFO")
        assert lark_logger.handlers == [] and lark_logger.propagate
        lark_logger.setLevel(logging.INFO)  # what lark-oapi does on every Client.build()
        lark_logger.info("connected to wss://example?device_id=abc")
        lark_logger.error("connect failed, err: boom")
        text = path.read_text(encoding="utf-8")
        assert text.count("connect failed") == 1 and "device_id" not in text
        setup_logging(tmp_path / "ws", "DEBUG")
        lark_logger.info("trying to reconnect")
        assert "trying to reconnect" in path.read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        for f in list(lark_logger.filters):
            lark_logger.removeFilter(f)
        lark_logger.setLevel(saved_level)
        for h in saved_handlers:
            lark_logger.addHandler(h)


def test_broken_brand_yaml_is_reported_in_italian(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import run
    from wiseglow_desk.docgen.brand import BrandError

    bad = tmp_path / "brand.yaml"
    bad.write_text("company: [\n", encoding="utf-8")
    with pytest.raises(BrandError, match="YAML"):
        Brand.from_yaml(bad)
    check = check_brand(make_settings(tmp_path, brand_path=bad))
    assert check.status == "fail" and "YAML" in check.detail

    monkeypatch.setenv("WISEGLOW_BRAND", str(bad))
    code = run.main(["--render", str(ROOT / "samples" / "nbo.json"), "--out", str(tmp_path / "r"), "--no-pdf"])
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("Rendering fallito:") and "Traceback" not in err


def test_serve_with_broken_brand_exits_with_one_line_error(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import run

    bad = tmp_path / "brand.yaml"
    bad.write_text("company: [\n", encoding="utf-8")
    monkeypatch.setattr(run, "setup_logging", lambda workspace, level: Path(workspace) / "logs" / "desk.log")
    code = run.cmd_serve(make_settings(tmp_path, brand_path=bad))
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("Brand non valido:") and "YAML" in err and "Traceback" not in err


def test_env_example_ships_no_usable_secrets() -> None:
    from dotenv import dotenv_values

    values = {k: v or "" for k, v in dotenv_values(ROOT / ".env.example").items()}
    assert values["FEISHU_APP_ID"] == values["FEISHU_APP_SECRET"] == values["ANTHROPIC_API_KEY"] == ""
    with pytest.raises(ConfigError, match="FEISHU_APP_ID"):
        settings_from_env(values)
    offline = settings_from_env(values, require_feishu=False)
    assert offline.anthropic_api_key is None
    assert check_anthropic_key(offline).status == "fail"


def test_install_script_dry_run_flags_world_readable_env(tmp_path: Path) -> None:
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash non disponibile")
    script = ROOT / "install-mac.sh"
    assert subprocess.run([bash, "-n", str(script)], capture_output=True).returncode == 0

    project = tmp_path / "proj"
    project.mkdir()
    for name in ("run.py", "requirements.txt", ".env.example"):
        shutil.copy(ROOT / name, project / name)
    env_file = project / ".env"
    env_file.write_text("FEISHU_APP_ID=x\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    cmd = [bash, str(script), "--dry-run", "--dir", str(project), "--python", sys.executable]

    def run_dry() -> str:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout + proc.stderr

    env_file.chmod(0o644)
    out = run_dry()
    assert "leggibile da altri utenti" in out and "chmod 600" in out
    assert env_file.stat().st_mode & 0o777 == 0o644  # dry-run changes nothing

    env_file.chmod(0o600)
    out = run_dry()
    assert ".env presente" in out and "chmod 600" not in out
