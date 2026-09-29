"""ClaudeBrain: one conversational turn per Feishu message via the Agent SDK.

``run_turn`` builds the user prompt (sender, buffered group context,
attachments, text), the :class:`ClaudeAgentOptions` (custom system prompt,
in-process ``wiseglow`` MCP tools, ``dontAsk`` permissions, per-chat resume)
and iterates ``claude_agent_sdk.query``.  The query function is injectable so
tests can feed scripted messages without spawning the CLI.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from ..docgen.brand import Brand, load_brand
from .prompt import build_system_prompt
from .tools import ToolContext, build_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Settings
    from ..docgen.store import DocumentStore
    from ..feishu.client import FeishuClient
    from ..feishu.events import IncomingMessage
    from .sessions import ChatState

log = logging.getLogger(__name__)

QueryFn = Callable[..., AsyncIterator[Any]]

BASE_TOOLS: tuple[str, ...] = (
    "Read",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "WebSearch",
    "WebFetch",
    "Agent",
    "TodoWrite",
)
MCP_TOOLS_PATTERN = "mcp__wiseglow__*"
# File tools are allowed only inside the workspace and the brand folder (path
# rules); the rest of BASE_TOOLS is allowed whole.  Glob and Grep are governed
# by the Read rules, so they are never listed bare.
SCOPED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write")
WHOLE_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch", "Agent", "TodoWrite")
# Secrets the CLI subprocess must not inherit from os.environ (load_dotenv
# puts them there); an explicit empty value in options.env overrides them.
SCRUBBED_ENV: tuple[str, ...] = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")

_LOST_SESSION_MARKERS: tuple[str, ...] = (
    "no conversation found",
    "session not found",
    "not found with session",
    "session file not found",
    "no session found",
)


@dataclass
class TurnResult:
    """Outcome of one turn; ``error`` is an Italian explanation when set."""

    text: str
    session_id: str | None
    cost_usd: float | None
    error: str | None
    duration_ms: int
    last_interim_text: str | None = None
    sent_files: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class _Collected:
    """What one ``query`` iteration produced."""

    final_texts: list[str] = field(default_factory=list)
    result: ResultMessage | None = None
    exception: BaseException | None = None
    # Kind of a failed API call ("rate_limit", "server_error", ...) when the
    # CLI emitted its synthetic error message instead of the model's text.
    api_error_kind: str | None = None

    def take(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            if message.parent_tool_use_id is not None:
                return  # subagent output
            if message.error:
                self.api_error_kind = str(message.error)
                return  # CLI-formatted API error prose, not the model's reply
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self.final_texts = []  # only text after the last tool use counts
                elif isinstance(block, TextBlock) and block.text.strip():
                    self.final_texts.append(block.text.strip())
        elif isinstance(message, ResultMessage):
            self.result = message

    @property
    def final_text(self) -> str:
        return "\n\n".join(self.final_texts).strip()

    def mentions_lost_session(self) -> bool:
        haystack = " ".join(
            s for s in (
                str(self.exception) if self.exception else "",
                self.result.result if self.result and self.result.result else "",
                " ".join(self.result.errors or []) if self.result else "",
            ) if s
        ).lower()
        return any(marker in haystack for marker in _LOST_SESSION_MARKERS)

    @property
    def api_error_status(self) -> int | None:
        """HTTP status of the failing API call, from the result or the trailing error."""
        if self.result is not None and self.result.api_error_status:
            return self.result.api_error_status
        return getattr(self.exception, "api_error_status", None)

    def is_api_failure(self) -> bool:
        """True when the run ended on an Anthropic API error (not a terminal subtype).

        The CLI reports it as ``subtype="success"`` with ``is_error=True``,
        ``terminal_reason="api_error"`` and the HTTP status; ``result`` then
        holds English error prose, never the model's reply.
        """
        if self.api_error_status or self.api_error_kind:
            return True
        reason = self.result.terminal_reason if self.result else getattr(self.exception, "terminal_reason", None)
        if reason == "api_error":
            return True
        return bool(self.result and self.result.is_error and self.result.subtype == "success")


class ClaudeBrain:
    """Runs Claude turns for Feishu chats through the Agent SDK."""

    def __init__(
        self,
        settings: "Settings",
        brand: Brand,
        workspace: Path,
        feishu_client: "FeishuClient",
        store: "DocumentStore",
        query_fn: QueryFn = query,
    ) -> None:
        self.settings = settings
        self.brand = brand
        self.workspace = Path(workspace).resolve()
        self.feishu = feishu_client
        self.store = store
        self._query = query_fn
        self.brand_path = Path(getattr(settings, "brand_path", "brand/brand.yaml")).resolve()

    # -- public --------------------------------------------------------------

    async def run_turn(
        self,
        chat: "ChatState",
        msg: "IncomingMessage",
        attachment_paths: list[Path],
        context: list[dict[str, Any]],
    ) -> TurnResult:
        """Run one turn for ``msg`` in ``chat`` and return the reply."""
        start = time.monotonic()
        self._refresh_brand()
        prompt = build_user_prompt(msg, attachment_paths, context)
        ctx = self.tool_context(chat, msg)
        resume = chat.session_id
        try:
            collected = await self._collect(prompt, self.build_options(ctx, resume))
            if resume and collected.mentions_lost_session():
                log.warning("Sessione %s non ripristinabile per %s: riparto da zero", resume, chat.chat_id)
                chat.session_id = None
                chat.save()
                collected = await self._collect(prompt, self.build_options(ctx, None))
        except Exception as exc:  # noqa: BLE001 - the bot must always get a TurnResult
            log.exception("Turno fallito per %s", chat.chat_id)
            return TurnResult(
                text="",
                session_id=chat.session_id,
                cost_usd=None,
                error=_describe_exception(exc),
                duration_ms=_elapsed_ms(start),
                last_interim_text=ctx.last_interim_text,
                sent_files=list(ctx.sent_files),
            )
        return self._to_result(collected, chat, ctx, start)

    def tool_context(self, chat: "ChatState", msg: "IncomingMessage") -> ToolContext:
        """The per-turn runtime handed to the MCP tools."""
        return ToolContext(
            store=self.store,
            brand_path=self.brand_path,
            feishu=self.feishu,
            chat_id=chat.chat_id,
            workspace=self.workspace,
            inbox_dir=self.workspace / "inbox" / chat.chat_id,
            knowledge_dir=self.workspace / "knowledge",
            reply_to_message_id=msg.message_id,
            created_by=msg.sender_name or msg.sender_open_id or "",
        )

    def build_options(self, ctx: ToolContext, resume: str | None) -> ClaudeAgentOptions:
        """The :class:`ClaudeAgentOptions` for one query (spec §Brain contract).

        Departures from the spec's literal list, both for its stated boundary
        ("non toccare altro sul computer"): file tools are allowed through
        path rules instead of bare names, and the prompt is delivered verbatim
        so ``@/path`` mentions or slash commands typed in the group are not
        expanded by the CLI.
        """
        s = self.settings
        allow_bash = bool(getattr(s, "allow_bash", False))
        tools = list(BASE_TOOLS) + (["Bash"] if allow_bash else [])
        env: dict[str, str] = {name: "" for name in SCRUBBED_ENV}
        api_key = getattr(s, "anthropic_api_key", None)
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        brand_dir = (self.brand.base_dir or self.brand_path.parent).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            model=s.model,
            effort=s.effort,
            system_prompt=build_system_prompt(self.brand, s, self.workspace),
            cwd=str(self.workspace),
            add_dirs=[str(brand_dir)],
            tools=tools,
            allowed_tools=allowed_tool_rules((self.workspace, brand_dir), allow_bash),
            permission_mode="dontAsk",
            mcp_servers={"wiseglow": build_tools(ctx)},
            strict_mcp_config=True,
            setting_sources=[],
            resume=resume,
            max_turns=s.max_turns,
            max_budget_usd=s.max_budget_usd_per_turn,
            env=env,
            verbatim_prompts=True,
        )

    # -- internals -----------------------------------------------------------

    def _refresh_brand(self) -> None:
        """Re-read brand.yaml so ``update_brand`` edits reach the next prompt."""
        if self.brand_path.is_file():
            try:
                self.brand = load_brand(self.brand_path)
            except Exception as exc:  # noqa: BLE001 - keep the last good brand
                log.warning("brand.yaml non ricaricato: %s", exc)

    async def _collect(self, prompt: str, options: ClaudeAgentOptions) -> _Collected:
        """Iterate one query; a trailing ResultError/ProcessError is kept, not raised."""
        collected = _Collected()
        try:
            async for message in self._query(prompt=prompt, options=options):
                collected.take(message)
        except (ResultError, ProcessError) as exc:
            collected.exception = exc
            log.warning("Claude ha chiuso il turno con errore: %s", _short(str(exc)))
        return collected

    def _to_result(self, collected: _Collected, chat: "ChatState", ctx: ToolContext, start: float) -> TurnResult:
        result = collected.result
        session_id = result.session_id if result and result.session_id else chat.session_id
        if session_id != chat.session_id:
            chat.session_id = session_id
            chat.save()
        cost = result.total_cost_usd if result else None
        chat.last_cost_usd = cost
        error = self._error_for(collected)
        text = collected.final_text or ((result.result or "") if result and not result.is_error else "")
        if error and collected.is_api_failure():
            text = ""  # the CLI's English error prose is not a reply
        elif error and not text and result and result.result and result.subtype != "success":
            text = result.result.strip()  # terminal subtypes keep the model's last text
        return TurnResult(
            text=text,
            session_id=session_id,
            cost_usd=cost,
            error=error,
            duration_ms=_elapsed_ms(start),
            last_interim_text=ctx.last_interim_text,
            sent_files=list(ctx.sent_files),
        )

    def _error_for(self, collected: _Collected) -> str | None:
        """Map an error result (or trailing exception) to a short Italian explanation."""
        result = collected.result
        subtype = result.subtype if result else None
        if isinstance(collected.exception, ResultError) and collected.exception.subtype:
            subtype = subtype or collected.exception.subtype
        if result is None and collected.exception is None:
            return "Claude non ha restituito alcun risultato."
        is_error = bool(result and (result.is_error or result.subtype.startswith("error_")))
        if not is_error and collected.exception is None:
            return None
        if subtype == "error_max_budget_usd":
            return (
                f"ho superato il budget massimo per un singolo turno "
                f"({self.settings.max_budget_usd_per_turn:.2f} USD): riprova con una richiesta più piccola "
                "o alza WISEGLOW_MAX_BUDGET_USD."
            )
        if subtype == "error_max_turns":
            return (
                f"ho raggiunto il numero massimo di passaggi ({self.settings.max_turns}) senza finire: "
                "riprova spezzando la richiesta o alza WISEGLOW_MAX_TURNS."
            )
        detail = _short(_result_detail(collected))
        if collected.is_api_failure():
            log.warning("Errore API Anthropic (stato %s, tipo %s): %s", collected.api_error_status, collected.api_error_kind, detail)
            return _describe_api_failure(collected.api_error_status, collected.api_error_kind)
        return "errore durante l'elaborazione" + (f": {detail}" if detail else ".")


# ---------------------------------------------------------------------------
# prompt building
# ---------------------------------------------------------------------------


def build_user_prompt(
    msg: "IncomingMessage",
    attachment_paths: list[Path],
    context: list[dict[str, Any]],
) -> str:
    """Header line, optional group context, optional attachments, then the text."""
    sender = msg.sender_name or msg.sender_open_id or "utente"
    where = "chat privata" if msg.chat_type == "p2p" else "gruppo"
    parts = [f"[Messaggio di {sender} in {where}, id {msg.message_id}]"]
    if context:
        lines = [
            f"[{c.get('time', '')}] {c.get('sender', '?')}: {c.get('text', '')}"
            + (f" (allegati: {', '.join(c['attachments'])})" if c.get("attachments") else "")
            for c in context
        ]
        parts.append("<contesto_gruppo>\n" + "\n".join(lines) + "\n</contesto_gruppo>")
    if attachment_paths:
        items = [f"- {Path(p).resolve()} ({_size_label(Path(p))})" for p in attachment_paths]
        parts.append("<allegati>\n" + "\n".join(items) + "\n</allegati>")
    text = (msg.text or "").strip()
    parts.append(text if text else "(nessun testo: solo allegati)")
    return "\n\n".join(parts)


def _size_label(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "dimensione sconosciuta"
    return f"{size / 1024:.1f} KB" if size < 1_048_576 else f"{size / 1_048_576:.1f} MB"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _result_detail(collected: _Collected) -> str:
    result = collected.result
    if result is not None:
        if result.errors:
            return "; ".join(result.errors)
        if result.result:
            return result.result
        if result.api_error_status:
            return f"errore API {result.api_error_status}"
    if collected.exception is not None:
        return str(collected.exception)
    return ""


_API_OVERLOADED = "il servizio Anthropic è momentaneamente sovraccarico, riprova tra poco."
_API_RATE_LIMIT = "limite di richieste API raggiunto, riprova tra qualche minuto."
_API_AUTH = "chiave API non valida o senza credito: controlla ANTHROPIC_API_KEY."


def _describe_api_failure(status: int | None, kind: str | None) -> str:
    """Short Italian explanation of a failed Anthropic API call (raw detail stays in the log)."""
    if status == 429 or kind == "rate_limit":
        return _API_RATE_LIMIT
    if status in (401, 403) or kind in ("authentication_failed", "billing_error"):
        return _API_AUTH
    if (status is not None and status >= 500) or kind == "server_error":
        return _API_OVERLOADED
    if status is not None:
        return f"l'API Anthropic ha rifiutato la richiesta (errore {status}); riprova o controlla i log."
    return "errore dell'API Anthropic; riprova tra poco o controlla i log."


def allowed_tool_rules(roots: Iterable[Path], allow_bash: bool) -> list[str]:
    """Permission rules: file tools confined to ``roots``, the other tools whole.

    ``Read(//abs/path/**)`` is the CLI's absolute-path rule syntax (the ``//``
    prefix marks an absolute path).  Under ``dontAsk`` anything not matched by
    a rule is denied, so a bare ``Read`` would allow any file on the machine.
    """
    rules: list[str] = []
    for root in roots:
        resolved = Path(root).resolve()
        if "," in str(resolved):  # the CLI receives the rules as a comma-joined list
            log.warning("Percorso con virgola, regole di permesso inaffidabili: %s", resolved)
        rules += [f"{tool}(//{str(resolved).lstrip('/')}/**)" for tool in SCOPED_TOOLS]
    rules += list(WHOLE_TOOLS) + (["Bash"] if allow_bash else [])
    return rules + [MCP_TOOLS_PATTERN]


def _describe_exception(exc: BaseException) -> str:
    if isinstance(exc, CLINotFoundError):
        return "Claude Code non è installato o non è raggiungibile su questo computer."
    if isinstance(exc, CLIConnectionError):
        return f"non riesco a collegarmi a Claude Code: {_short(str(exc))}"
    if isinstance(exc, ProcessError):
        return f"il processo Claude si è interrotto: {_short(str(exc))}"
    return f"errore inatteso: {_short(str(exc))}"
