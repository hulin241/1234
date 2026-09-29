"""Tests for agent/sessions.py (ChatState, SessionRegistry) and agent/brain.py (ClaudeBrain.run_turn)."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from wiseglow_desk.agent.brain import (  # noqa: E402
    BASE_TOOLS,
    ClaudeBrain,
    TurnResult,
    allowed_tool_rules,
    build_user_prompt,
)
from wiseglow_desk.agent.sessions import (  # noqa: E402
    DEDUPE_SIZE,
    MAX_CONTEXT_ENTRIES,
    ChatState,
    SessionRegistry,
)
from wiseglow_desk.docgen.brand import Brand  # noqa: E402
from wiseglow_desk.feishu.events import Attachment, IncomingMessage  # noqa: E402

try:  # the BOT agent owns config.py; fall back to a stub with the same fields
    from wiseglow_desk.config import Settings  # type: ignore
except Exception:  # noqa: BLE001

    @dataclass(frozen=True)
    class Settings:  # type: ignore[no-redef]
        feishu_app_id: str = "cli_x"
        feishu_app_secret: str = "secret"
        feishu_domain: str = "feishu"
        anthropic_api_key: str | None = None
        model: str = "claude-fable-5-1"
        effort: str = "high"
        workspace_dir: Path = Path("workspace")
        brand_path: Path = Path("brand/brand.yaml")
        trigger_mode: str = "mention"
        allow_bash: bool = False
        max_budget_usd_per_turn: float = 5.0
        max_turns: int = 60
        max_parallel_chats: int = 2
        allowed_chat_ids: tuple[str, ...] = ()
        log_level: str = "INFO"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_msg(
    text: str = "ciao",
    message_id: str = "om_1",
    chat_id: str = "oc_group",
    sender_name: str | None = "Mario",
    chat_type: str = "group",
    attachments: list[Attachment] | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,  # type: ignore[arg-type]
        sender_open_id="ou_mario",
        sender_name=sender_name,
        text=text,
        mentions_bot=True,
        mentions_others=[],
        parent_id=None,
        root_id=None,
        attachments=attachments or [],
        message_type="text",
        create_time_ms=1_760_000_000_000,
        raw_content="",
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# ChatState persistence
# ---------------------------------------------------------------------------


def test_chat_state_save_and_load_session(tmp_path: Path) -> None:
    state = ChatState("oc_1", tmp_path / "chats" / "oc_1")
    assert state.session_id is None
    state.session_id = "sess-abc"
    state.save()

    data = json.loads((tmp_path / "chats" / "oc_1" / "session.json").read_text(encoding="utf-8"))
    assert data["session_id"] == "sess-abc"
    assert data["updated"]

    fresh = ChatState("oc_1", tmp_path / "chats" / "oc_1")
    fresh.load()
    assert fresh.session_id == "sess-abc"
    assert fresh.updated == data["updated"]


def test_chat_state_load_ignores_corrupt_file(tmp_path: Path) -> None:
    root = tmp_path / "chats" / "oc_1"
    root.mkdir(parents=True)
    (root / "session.json").write_text("{not json", encoding="utf-8")
    state = ChatState("oc_1", root)
    state.load()
    assert state.session_id is None


def test_chat_state_reset_clears_session(tmp_path: Path) -> None:
    state = ChatState("oc_1", tmp_path / "chats" / "oc_1")
    state.session_id = "sess"
    state.save()
    state.reset()
    assert state.session_id is None
    assert json.loads(state.session_path.read_text(encoding="utf-8"))["session_id"] is None


def test_chat_state_has_lock_and_queue(tmp_path: Path) -> None:
    state = ChatState("oc_1", tmp_path / "chats" / "oc_1")
    assert isinstance(state.lock, asyncio.Lock)
    assert isinstance(state.queue, asyncio.Queue)


# ---------------------------------------------------------------------------
# context buffer
# ---------------------------------------------------------------------------


def test_context_append_and_drain(tmp_path: Path) -> None:
    state = ChatState("oc_1", tmp_path / "chats" / "oc_1")
    state.append_context(make_msg("primo", message_id="om_a"), [])
    state.append_context(make_msg("secondo", message_id="om_b", sender_name="Anna"), [tmp_path / "x.pdf"])

    lines = state.context_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    entries = state.drain_context()
    assert [e["text"] for e in entries] == ["primo", "secondo"]
    assert entries[0]["sender"] == "Mario"
    assert entries[1]["sender"] == "Anna"
    assert entries[1]["attachments"] == [str(tmp_path / "x.pdf")]
    assert entries[0]["time"]
    assert entries[0]["message_id"] == "om_a"
    # drained: buffer is empty and the file is gone
    assert state.drain_context() == []
    assert not state.context_path.exists()


def test_context_keeps_only_newest_entries(tmp_path: Path) -> None:
    state = ChatState("oc_1", tmp_path / "chats" / "oc_1")
    for i in range(MAX_CONTEXT_ENTRIES + 15):
        state.append_context(make_msg(f"msg {i}", message_id=f"om_{i}"), [])
    entries = state.drain_context()
    assert len(entries) == MAX_CONTEXT_ENTRIES
    assert entries[0]["text"] == "msg 15"
    assert entries[-1]["text"] == f"msg {MAX_CONTEXT_ENTRIES + 14}"


def test_context_uses_open_id_when_name_unknown(tmp_path: Path) -> None:
    state = ChatState("oc_1", tmp_path / "chats" / "oc_1")
    state.append_context(make_msg("x", sender_name=None), [])
    assert state.drain_context()[0]["sender"] == "ou_mario"


# ---------------------------------------------------------------------------
# registry + dedupe
# ---------------------------------------------------------------------------


def test_registry_returns_same_state_and_loads_from_disk(tmp_path: Path) -> None:
    registry = SessionRegistry(tmp_path)
    state = registry.get("oc_1")
    assert registry.get("oc_1") is state
    assert state.root == tmp_path / "chats" / "oc_1"
    state.session_id = "s1"
    state.save()

    other = SessionRegistry(tmp_path).get("oc_1")
    assert other.session_id == "s1"
    assert len(registry) == 1


def test_registry_sanitises_chat_dir(tmp_path: Path) -> None:
    state = SessionRegistry(tmp_path).get("../weird id")
    assert state.root.parent == tmp_path / "chats"
    assert "/" not in state.root.name and ".." not in state.root.name


def test_seen_message_dedupes_and_evicts(tmp_path: Path) -> None:
    registry = SessionRegistry(tmp_path)
    assert registry.seen_message("om_1") is False
    assert registry.seen_message("om_1") is True
    assert registry.seen_message("om_2") is False

    for i in range(DEDUPE_SIZE + 10):
        registry.seen_message(f"bulk_{i}")
    # the oldest ids were evicted, the newest are still known
    assert registry.seen_message("om_1") is False
    assert registry.seen_message(f"bulk_{DEDUPE_SIZE + 9}") is True


# ---------------------------------------------------------------------------
# brain: scripted query function
# ---------------------------------------------------------------------------


def result_msg(
    subtype: str = "success",
    session_id: str = "sess-1",
    is_error: bool = False,
    result: str | None = None,
    cost: float | None = 0.1234,
    errors: list[str] | None = None,
    api_error_status: int | None = None,
    terminal_reason: str | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1500,
        duration_api_ms=1200,
        is_error=is_error,
        num_turns=3,
        session_id=session_id,
        total_cost_usd=cost,
        result=result,
        errors=errors,
        api_error_status=api_error_status,
        terminal_reason=terminal_reason,
    )


def assistant(*blocks: Any, parent: str | None = None, error: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=list(blocks), model="claude-fable-5-1", parent_tool_use_id=parent, error=error  # type: ignore[arg-type]
    )


def api_failure(status: int, prose: str, kind: str | None = None, *, with_exception: bool = True) -> list[Any]:
    """What the CLI emits when the run ends on an API error (see claude_agent_sdk/_internal/query.py)."""
    data = {"subtype": "success", "is_error": True, "errors": [], "result": prose, "api_error_status": status}
    script: list[Any] = [
        assistant(TextBlock(prose), error=kind),
        result_msg(subtype="success", is_error=True, result=prose, errors=[], api_error_status=status, terminal_reason="api_error"),
    ]
    if with_exception:
        script.append(ResultError(f"Claude Code returned an error result: {prose}", data=data, exit_code=1))
    return script


class ScriptedQuery:
    """Replaces ``claude_agent_sdk.query``: each call plays the next script."""

    def __init__(self, scripts: list[list[Any]]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> Any:
        self.calls.append({"prompt": prompt, "options": options})
        script = self.scripts.pop(0)
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item


class FakeFeishu:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.files: list[tuple[str, Path]] = []

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        self.texts.append((chat_id, text))
        return f"om_t{len(self.texts)}"

    async def send_file(self, chat_id: str, path: Path, *, reply_to: str | None = None) -> str | None:
        self.files.append((chat_id, Path(path)))
        return f"om_f{len(self.files)}"


def make_brain(tmp_path: Path, scripts: list[list[Any]], **settings_kw: Any) -> tuple[ClaudeBrain, ScriptedQuery, ChatState]:
    workspace = tmp_path / "workspace"
    brand_path = ROOT / "brand" / "brand.yaml"
    settings = Settings(brand_path=brand_path, workspace_dir=workspace, **settings_kw)
    scripted = ScriptedQuery(scripts)
    brain = ClaudeBrain(
        settings=settings,
        brand=Brand.from_yaml(brand_path),
        workspace=workspace,
        feishu_client=FakeFeishu(),  # type: ignore[arg-type]
        store=object(),  # type: ignore[arg-type]
        query_fn=scripted,
    )
    chat = SessionRegistry(workspace).get("oc_group")
    return brain, scripted, chat


def test_run_turn_success_takes_text_after_last_tool_use(tmp_path: Path) -> None:
    script = [
        assistant(TextBlock("Sto leggendo il file..."), ToolUseBlock(id="t1", name="Read", input={})),
        assistant(TextBlock("Ignorami: sono un subagente"), parent="t1"),
        assistant(TextBlock("Ora rendo il documento"), ToolUseBlock(id="t2", name="mcp__wiseglow__render_document", input={})),
        assistant(TextBlock("Fatto: NBO inviata in PDF."), TextBlock("Dimmi se vuoi cambiare il prezzo.")),
        result_msg(session_id="sess-new", result="Fatto: NBO inviata in PDF."),
    ]
    brain, scripted, chat = make_brain(tmp_path, [script], anthropic_api_key="sk-test")

    res: TurnResult = run(brain.run_turn(chat, make_msg("fammi una NBO"), [], []))

    assert res.error is None and res.ok
    assert res.text == "Fatto: NBO inviata in PDF.\n\nDimmi se vuoi cambiare il prezzo."
    assert res.session_id == "sess-new"
    assert res.cost_usd == pytest.approx(0.1234)
    assert res.duration_ms >= 0
    assert chat.session_id == "sess-new"
    assert json.loads(chat.session_path.read_text(encoding="utf-8"))["session_id"] == "sess-new"
    assert chat.last_cost_usd == pytest.approx(0.1234)
    assert len(scripted.calls) == 1


def test_run_turn_builds_options_per_spec(tmp_path: Path) -> None:
    brain, scripted, chat = make_brain(tmp_path, [[result_msg()]], anthropic_api_key="sk-test", allow_bash=True)
    chat.session_id = "sess-old"
    run(brain.run_turn(chat, make_msg("ciao"), [], []))

    opts: ClaudeAgentOptions = scripted.calls[0]["options"]
    base = ["Read", "Glob", "Grep", "Write", "Edit", "WebSearch", "WebFetch", "Agent", "TodoWrite", "Bash"]
    assert opts.tools == base
    workspace = (tmp_path / "workspace").resolve()
    brand_dir = (ROOT / "brand").resolve()
    scoped = [f"{t}(//{str(r).lstrip('/')}/**)" for r in (workspace, brand_dir) for t in ("Read", "Edit", "Write")]
    assert opts.allowed_tools == scoped + ["WebSearch", "WebFetch", "Agent", "TodoWrite", "Bash", "mcp__wiseglow__*"]
    assert opts.permission_mode == "dontAsk"
    assert opts.strict_mcp_config is True
    assert opts.setting_sources == []
    assert opts.verbatim_prompts is True
    assert opts.resume == "sess-old"
    assert opts.max_turns == 60
    assert opts.max_budget_usd == 5.0
    assert opts.model == "claude-fable-5-1"
    assert opts.effort == "high"
    assert isinstance(opts.system_prompt, str) and "WiseGlow" in opts.system_prompt
    assert opts.cwd == str(workspace)
    assert opts.add_dirs == [str(brand_dir)]
    assert opts.env == {"ANTHROPIC_API_KEY": "sk-test", "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""}
    assert set(opts.mcp_servers) == {"wiseglow"}
    assert opts.mcp_servers["wiseglow"]["type"] == "sdk"


def test_run_turn_without_api_key_and_bash(tmp_path: Path) -> None:
    brain, scripted, chat = make_brain(tmp_path, [[result_msg()]])
    run(brain.run_turn(chat, make_msg("ciao"), [], []))
    opts = scripted.calls[0]["options"]
    assert "Bash" not in opts.tools and "Bash" not in opts.allowed_tools
    # Feishu secrets from .env are blanked for the CLI subprocess (explicit env wins over inherited)
    assert opts.env == {"FEISHU_APP_ID": "", "FEISHU_APP_SECRET": ""}
    assert opts.resume is None


def test_allowed_tools_confine_file_tools_to_roots(tmp_path: Path) -> None:
    """No bare Read/Write/Edit/Glob/Grep: under dontAsk a bare name allows the whole tool anywhere."""
    rules = allowed_tool_rules((tmp_path / "ws", tmp_path / "brand"), allow_bash=False)
    bare = {r for r in rules if "(" not in r}
    assert bare == {"WebSearch", "WebFetch", "Agent", "TodoWrite", "mcp__wiseglow__*"}
    assert not bare & {"Read", "Write", "Edit", "Glob", "Grep"}
    ws = str((tmp_path / "ws").resolve()).lstrip("/")
    assert f"Read(//{ws}/**)" in rules and f"Write(//{ws}/**)" in rules and f"Edit(//{ws}/**)" in rules
    assert all(r.startswith(("Read(", "Write(", "Edit(")) or r in bare for r in rules)
    # every tool the CLI is given still has a rule (tools=BASE_TOOLS stays intact)
    for name in BASE_TOOLS:
        assert any(r == name or r.startswith(f"{name}(") for r in rules) or name in ("Glob", "Grep")
    assert "Bash" in allowed_tool_rules((tmp_path,), allow_bash=True)


def test_run_turn_falls_back_to_result_text(tmp_path: Path) -> None:
    brain, _, chat = make_brain(tmp_path, [[result_msg(result="Risposta dal result")]])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.text == "Risposta dal result"
    assert res.error is None


def test_run_turn_maps_budget_error(tmp_path: Path) -> None:
    script = [
        assistant(TextBlock("Sto lavorando"), ToolUseBlock(id="t1", name="Read", input={})),
        result_msg(subtype="error_max_budget_usd", is_error=True, session_id="sess-b"),
    ]
    brain, _, chat = make_brain(tmp_path, [script])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.error is not None and "budget" in res.error
    assert res.session_id == "sess-b"
    assert chat.session_id == "sess-b"  # still resumable


def test_run_turn_maps_max_turns_error(tmp_path: Path) -> None:
    brain, _, chat = make_brain(tmp_path, [[result_msg(subtype="error_max_turns", is_error=True)]])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.error is not None and "passaggi" in res.error and "60" in res.error


def test_run_turn_generic_error_keeps_result_text(tmp_path: Path) -> None:
    """A terminal subtype keeps the model's last text (result) as the reply."""
    script = [result_msg(subtype="error_during_execution", is_error=True, result="Ho letto la visura, poi mi sono bloccato.")]
    brain, _, chat = make_brain(tmp_path, [script])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.error is not None and res.error.startswith("errore durante l'elaborazione")
    assert "bloccato" in res.error
    assert res.text == "Ho letto la visura, poi mi sono bloccato."


def test_run_turn_api_overloaded_is_italian_and_not_a_reply(tmp_path: Path) -> None:
    prose = "API Error: 529 Overloaded. This is a server-side issue, please retry."
    brain, _, chat = make_brain(tmp_path, [api_failure(529, prose, "server_error")])
    res = run(brain.run_turn(chat, make_msg("fammi una NBO"), [], []))
    assert res.text == ""  # the CLI's English prose must not be posted as Claude's answer
    assert res.error == "il servizio Anthropic è momentaneamente sovraccarico, riprova tra poco."
    assert "API Error" not in res.error and "529" not in res.error
    assert res.session_id == "sess-1" and chat.session_id == "sess-1"


def test_run_turn_api_failure_after_tool_use_discards_prose(tmp_path: Path) -> None:
    script = [
        assistant(TextBlock("Leggo il file"), ToolUseBlock(id="t1", name="Read", input={})),
        *api_failure(429, "API Error: 429 rate limited", "rate_limit"),
    ]
    brain, _, chat = make_brain(tmp_path, [script])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.text == ""
    assert res.error is not None and "limite di richieste" in res.error


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (401, "authentication_failed", "ANTHROPIC_API_KEY"),
        (403, "billing_error", "ANTHROPIC_API_KEY"),
        (500, "server_error", "sovraccarico"),
        (400, "invalid_request", "errore 400"),
    ],
)
def test_run_turn_maps_api_statuses(tmp_path: Path, status: int, kind: str, expected: str) -> None:
    brain, _, chat = make_brain(tmp_path, [api_failure(status, f"API Error: {status} something", kind)])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.text == "" and res.error is not None and expected in res.error


def test_run_turn_api_failure_without_trailing_exception_or_error_kind(tmp_path: Path) -> None:
    """Older CLI shape: prose as a plain TextBlock, result subtype 'success' with is_error and no status."""
    prose = "Authentication error · invalid x-api-key"
    script = [assistant(TextBlock(prose)), result_msg(subtype="success", is_error=True, result=prose, errors=[])]
    brain, _, chat = make_brain(tmp_path, [script])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.text == ""
    assert res.error == "errore dell'API Anthropic; riprova tra poco o controlla i log."


def test_run_turn_error_result_then_result_error_exception(tmp_path: Path) -> None:
    data = {"subtype": "error_during_execution", "is_error": True, "errors": ["boom"]}
    script = [
        result_msg(subtype="error_during_execution", is_error=True, errors=["boom"]),
        ResultError("Claude Code returned an error result: boom", data=data, exit_code=1),
    ]
    brain, scripted, chat = make_brain(tmp_path, [script])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.error is not None and "boom" in res.error
    assert len(scripted.calls) == 1


def test_run_turn_retries_without_resume_when_session_is_gone(tmp_path: Path) -> None:
    lost = "No conversation found with session ID: sess-old"
    data = {"subtype": "error_during_execution", "is_error": True, "result": lost}
    first = [
        result_msg(subtype="error_during_execution", is_error=True, result=lost, session_id="sess-old"),
        ResultError(f"Claude Code returned an error result: {lost}", data=data, exit_code=1),
    ]
    second = [assistant(TextBlock("Ripartito da zero, ecco la risposta.")), result_msg(session_id="sess-fresh")]
    brain, scripted, chat = make_brain(tmp_path, [first, second])
    chat.session_id = "sess-old"
    chat.save()

    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))

    assert len(scripted.calls) == 2
    assert scripted.calls[0]["options"].resume == "sess-old"
    assert scripted.calls[1]["options"].resume is None
    assert res.error is None
    assert res.text == "Ripartito da zero, ecco la risposta."
    assert chat.session_id == "sess-fresh"
    assert json.loads(chat.session_path.read_text(encoding="utf-8"))["session_id"] == "sess-fresh"


def test_run_turn_retries_when_resume_error_is_raised_before_any_result(tmp_path: Path) -> None:
    first = [ResultError("No conversation found with session ID: sess-old", data={}, exit_code=1)]
    second = [assistant(TextBlock("ok")), result_msg(session_id="sess-2")]
    brain, scripted, chat = make_brain(tmp_path, [first, second])
    chat.session_id = "sess-old"
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert len(scripted.calls) == 2
    assert res.error is None and res.text == "ok" and chat.session_id == "sess-2"


def test_run_turn_does_not_retry_without_resume(tmp_path: Path) -> None:
    script = [ResultError("No conversation found with session ID: x", data={}, exit_code=1)]
    brain, scripted, chat = make_brain(tmp_path, [script])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert len(scripted.calls) == 1
    assert res.error is not None


def test_run_turn_cli_not_found_becomes_error(tmp_path: Path) -> None:
    brain, _, chat = make_brain(tmp_path, [[CLINotFoundError("Claude Code not found")]])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.error is not None and "Claude Code" in res.error
    assert res.text == ""


def test_run_turn_unexpected_exception_becomes_error(tmp_path: Path) -> None:
    brain, _, chat = make_brain(tmp_path, [[RuntimeError("kaboom")]])
    res = run(brain.run_turn(chat, make_msg("ciao"), [], []))
    assert res.error is not None and "kaboom" in res.error


# ---------------------------------------------------------------------------
# user prompt
# ---------------------------------------------------------------------------


def test_build_user_prompt_has_header_context_and_attachments(tmp_path: Path) -> None:
    pdf = tmp_path / "visura.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    context = [
        {"time": "2026-09-29 10:00", "sender": "Anna", "text": "il terreno è a Lecce", "attachments": []},
        {"time": "2026-09-29 10:01", "sender": "Luca", "text": "ecco la visura", "attachments": [str(pdf)]},
    ]
    prompt = build_user_prompt(make_msg("fammi la NBO"), [pdf], context)

    assert prompt.startswith("[Messaggio di Mario in gruppo")
    assert "<contesto_gruppo>" in prompt and "</contesto_gruppo>" in prompt
    assert "[2026-09-29 10:00] Anna: il terreno è a Lecce" in prompt
    assert "Luca: ecco la visura (allegati:" in prompt
    assert "<allegati>" in prompt and str(pdf.resolve()) in prompt and "KB" in prompt
    assert prompt.rstrip().endswith("fammi la NBO")


def test_build_user_prompt_minimal_p2p() -> None:
    prompt = build_user_prompt(make_msg("", chat_type="p2p", sender_name=None), [], [])
    assert "chat privata" in prompt and "ou_mario" in prompt
    assert "<contesto_gruppo>" not in prompt and "<allegati>" not in prompt
    assert "solo allegati" in prompt
