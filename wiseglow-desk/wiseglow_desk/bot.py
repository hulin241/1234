"""Desk: Feishu events → dedupe/filter → per-chat queue → Claude brain → replies.

Flow of one event:

1. ``on_ws_event`` (called in the long-connection thread) pushes the raw event
   to the asyncio loop with ``call_soon_threadsafe`` and returns at once.
2. ``handle_event`` parses it into an :class:`IncomingMessage`.
3. ``handle_message`` de-duplicates by ``message_id``, applies the allowed-chat
   filter and decides whether the message is *addressed* to the bot (DM,
   @mention, reply to one of our messages, or ``trigger_mode == "all"``).
   ``/help`` and ``/status`` are answered right there; everything else is
   queued for the chat, so the single dispatcher never waits on the network.
4. The per-chat worker always downloads attachments to ``inbox/<chat_id>/``
   (even for unaddressed messages, so "send me the file, then @ me" works),
   buffers unaddressed group messages as context and runs addressed ones as
   a Claude turn: serialised per chat, bounded globally by a semaphore and by
   a wall-clock timeout, with ``OnIt`` → ``DONE``/``ERROR`` reactions and the
   reply posted as an answer to the user's message.

The bot identity (``bot_open_id``) is resolved at startup and, if Feishu was
unreachable at that moment (a Mac booting before Wi-Fi), retried in the
background until it succeeds.

Collaborators (Feishu client, brain, registry, store, brand) are injectable so
the wiring can be tested with fakes and no network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from .agent.brain import ClaudeBrain, TurnResult
from .agent.sessions import ChatState, SessionRegistry
from .config import Settings
from .docgen.brand import Brand, BrandError, load_brand
from .docgen.docx_renderer import render_docx
from .docgen.model import Document
from .docgen.pdf import docx_to_pdf, find_soffice, pdf_page_count
from .docgen.store import DocumentStore
from .feishu.client import FeishuClient
from .feishu.events import IncomingMessage, parse_message_event
from .feishu.ws import LongConnection

log = logging.getLogger(__name__)

REACTION_WORKING = "OnIt"
REACTION_DONE = "DONE"
REACTION_ERROR = "ERROR"
COMMANDS: tuple[str, ...] = ("/reset", "/status", "/help")
SHUTDOWN_GRACE_S = 20.0
IDENTITY_RETRY_S = 30.0
WORKSPACE_SUBDIRS: tuple[str, ...] = ("chats", "inbox", "documents", "knowledge/templates", "logs")
NOTES_HEADER = (
    "# Note di lavoro (memoria di Claude per il team WiseGlow)\n\n"
    "Un punto elenco per voce: preferenze del team, clausole standard, decisioni prese, "
    "correzioni ricevute. Aggiorna le voci esistenti invece di duplicarle.\n\n"
)

HELP_TEXT = (
    "Ciao! Sono Claude, il collega del team WiseGlow per i documenti.\n"
    "Scrivimi in privato o menzionami nel gruppo e dimmi in parole tue cosa ti serve: "
    "una NBO, una LOI, una manifestazione di interesse per un terreno, un mandato, una proposta per la banca, "
    "un contratto (PPA, EPC, O&M, DDS, procacciatore…) o qualsiasi altro documento. Posso anche rispondere a "
    "domande e ragionare con te sui contenuti.\n"
    "Puoi mandarmi file (PDF, Word, Excel, immagini) e poi chiedermi di usarli; se un file è un modello, dimmelo "
    "e lo tengo come riferimento.\n"
    "Per modificare un documento basta chiederlo (ad esempio «nella NBO di Lecce porta il prezzo a 1,2 milioni»): "
    "creo una nuova versione con la grafica WiseGlow e te la mando in PDF (o Word, se lo chiedi).\n"
    "Comandi: /reset ricomincia la conversazione da zero, /status mostra lo stato di questa chat, /help questo messaggio."
)


# ---------------------------------------------------------------------------
# Protocols for injectable collaborators (real classes satisfy them)
# ---------------------------------------------------------------------------


class FeishuLike(Protocol):
    sent_message_ids: deque[str]
    bot_name: str | None

    async def bot_open_id(self) -> str | None: ...

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None: ...

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str | None: ...

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None: ...

    async def download_resource(
        self, message_id: str, key: str, kind: Literal["file", "image"], dest_dir: Path, name_hint: str
    ) -> Path: ...


class BrainLike(Protocol):
    async def run_turn(
        self, chat: ChatState, msg: IncomingMessage, attachment_paths: list[Path], context: list[dict[str, Any]]
    ) -> TurnResult: ...


class StoreLike(Protocol):
    def list(self, chat_id: str | None = None) -> list[dict[str, Any]]: ...


class ConnectionLike(Protocol):
    def start_in_thread(self) -> Any: ...

    def stop(self) -> None: ...


ConnectionFactory = Callable[[str, str, str, Callable[[Any], None]], ConnectionLike]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def ensure_workspace(workspace: Path) -> Path:
    """Create the runtime folders and the memory file (idempotent)."""
    workspace = Path(workspace)
    for sub in WORKSPACE_SUBDIRS:
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    notes = workspace / "knowledge" / "NOTES.md"
    if not notes.exists():
        notes.write_text(NOTES_HEADER, encoding="utf-8")
    return workspace


def _file_size(path: Path) -> str:
    """Human-readable size for logs ("1.2 MB"); never raises."""
    try:
        size = path.stat().st_size
    except OSError:
        return "dimensione sconosciuta"
    return f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{size / 1000:.1f} kB"


def parse_command(text: str) -> str | None:
    """Return the slash command (``/reset``, ``/status``, ``/help``) or None."""
    words = (text or "").strip().split(maxsplit=1)
    if not words:
        return None
    first = words[0].lower()
    return first if first in COMMANDS else None


# ---------------------------------------------------------------------------
# Desk
# ---------------------------------------------------------------------------


class Desk:
    """The running bot: one instance per process."""

    def __init__(
        self,
        settings: Settings,
        *,
        feishu: FeishuLike | None = None,
        brain: BrainLike | None = None,
        registry: SessionRegistry | None = None,
        store: StoreLike | None = None,
        brand: Brand | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.settings = settings
        self.workspace = ensure_workspace(Path(settings.workspace_dir))
        self.brand = brand if brand is not None else load_brand(settings.brand_path)
        self.feishu: FeishuLike = feishu if feishu is not None else FeishuClient(
            settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_domain
        )
        self.store: StoreLike = store if store is not None else DocumentStore(
            self.workspace / "documents", settings.brand_path
        )
        self.registry = registry if registry is not None else SessionRegistry(self.workspace)
        self.brain: BrainLike = brain if brain is not None else ClaudeBrain(
            settings, self.brand, self.workspace, self.feishu, self.store  # type: ignore[arg-type]
        )
        self._connection_factory: ConnectionFactory = connection_factory or LongConnection  # type: ignore[assignment]
        self._connection: ConnectionLike | None = None
        self.bot_open_id: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(max(1, settings.max_parallel_chats))
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._addressed: dict[str, bool] = {}  # message_id → addressed, for messages waiting in a chat queue
        self._identity_task: asyncio.Task[None] | None = None
        self.identity_retry_s = IDENTITY_RETRY_S
        self._stop = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Start the long connection and serve until SIGINT/SIGTERM."""
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        await self._resolve_identity()
        if self.bot_open_id is None:
            self._identity_task = asyncio.create_task(self._identity_loop(), name="desk-identity")
        s = self.settings
        self._connection = self._connection_factory(s.feishu_app_id, s.feishu_app_secret, s.feishu_domain, self.on_ws_event)
        self._connection.start_in_thread()
        log.info(
            "WiseGlow Desk avviato: modello %s (effort %s), modalità %s, workspace %s",
            s.model, s.effort, s.trigger_mode, self.workspace,
        )
        dispatcher = asyncio.create_task(self._dispatch_forever(), name="desk-dispatcher")
        await self._stop.wait()
        await self._shutdown(dispatcher)

    def request_stop(self) -> None:
        """Ask ``run`` to return (safe to call from a signal handler)."""
        log.info("Richiesta di arresto ricevuta")
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        assert self._loop is not None
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):  # e.g. Windows or non-main thread
                log.debug("Signal handler per %s non installato", sig)

    async def _resolve_identity(self) -> None:
        self.bot_open_id = await self.feishu.bot_open_id()
        if self.bot_open_id is None:
            log.warning(
                "Impossibile leggere l'identità del bot: riprovo ogni %.0fs; nel frattempo le menzioni "
                "verranno riconosciute per nome/tipo", self.identity_retry_s,
            )
        if not self.settings.has_anthropic_key:
            log.warning("ANTHROPIC_API_KEY assente: i turni di Claude falliranno finché non la imposti nel .env")

    async def _identity_loop(self) -> None:
        """Keep asking Feishu for the bot identity until it answers (the client caches on success)."""
        while self.bot_open_id is None and not self._stop.is_set():
            await asyncio.sleep(self.identity_retry_s)
            try:
                self.bot_open_id = await self.feishu.bot_open_id()
            except Exception as exc:  # noqa: BLE001 - the client returns None on failure, but never trust it
                log.warning("Lettura dell'identità del bot fallita: %s", exc)
        if self.bot_open_id is not None:
            log.info("Identità del bot risolta: «%s» (%s)", self.feishu.bot_name or "?", self.bot_open_id)

    async def _shutdown(self, dispatcher: asyncio.Task[None]) -> None:
        log.info("Arresto in corso: chiudo la connessione e attendo i turni in corso (max %.0fs)", SHUTDOWN_GRACE_S)
        if self._connection is not None:
            self._connection.stop()
        background = [t for t in (dispatcher, self._identity_task) if t is not None]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        try:
            await asyncio.wait_for(self.drain(), timeout=SHUTDOWN_GRACE_S)
        except asyncio.TimeoutError:
            log.warning("Turni ancora in corso dopo %.0fs: li interrompo", SHUTDOWN_GRACE_S)
        for task in self._workers.values():
            task.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        log.info("WiseGlow Desk fermato")

    async def drain(self) -> None:
        """Wait until every queued message has been processed (tests, shutdown)."""
        queues = [self.registry.get(chat_id).queue for chat_id in list(self._workers)]
        await asyncio.gather(*(q.join() for q in queues))

    # -- event intake ----------------------------------------------------------

    def on_ws_event(self, event: Any) -> None:
        """Called in the WS thread: hand the event to the loop and return immediately."""
        loop = self._loop
        if loop is None or loop.is_closed():
            log.warning("Evento Feishu ricevuto senza loop attivo: ignorato")
            return
        loop.call_soon_threadsafe(self._events.put_nowait, event)

    async def _dispatch_forever(self) -> None:
        while True:
            event = await self._events.get()
            try:
                await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad event must not stop the bot
                log.exception("Errore nella gestione di un evento Feishu")

    async def handle_event(self, event: Any) -> str:
        """Parse a raw ``P2ImMessageReceiveV1`` and route it."""
        try:
            msg = parse_message_event(event, self.bot_open_id, self.feishu.bot_name)
        except ValueError as exc:
            log.warning("Evento ignorato: %s", exc)
            return "unparsable"
        return await self.handle_message(msg)

    async def handle_message(self, msg: IncomingMessage) -> str:
        """Route one parsed message; returns a short outcome label (for logs/tests).

        Only cheap local work happens here, because the dispatcher handles one
        event at a time: downloads and turns run in the per-chat worker, so a
        slow attachment in one chat never delays the others.
        """
        if self.registry.seen_message(msg.message_id):
            log.debug("Messaggio duplicato %s ignorato", msg.message_id)
            return "duplicate"
        if not self.settings.chat_allowed(msg.chat_id):
            log.info("Chat %s non abilitata: messaggio ignorato", msg.chat_id)
            return "chat_not_allowed"

        chat = self.registry.get(msg.chat_id)
        addressed = self.is_addressed(msg)
        command = parse_command(msg.text) if addressed else None
        if command in ("/help", "/status"):
            await self._reply_command(chat, msg, command)
            return "command"

        self._addressed[msg.message_id] = addressed
        await chat.queue.put(msg)
        self._ensure_worker(chat)
        return "queued" if addressed else "context"

    def is_addressed(self, msg: IncomingMessage) -> bool:
        """DM, @mention, reply to one of our messages, or trigger_mode == "all"."""
        if msg.chat_type == "p2p" or msg.mentions_bot:
            return True
        if msg.parent_id and msg.parent_id in self.feishu.sent_message_ids:
            return True
        return self.settings.trigger_mode == "all"

    async def _download_attachments(self, msg: IncomingMessage) -> tuple[list[Path], list[str]]:
        """Download every attachment to ``inbox/<chat_id>/``; return (paths, failed names)."""
        dest = self.workspace / "inbox" / msg.chat_id
        paths: list[Path] = []
        failed: list[str] = []
        for att in msg.attachments:
            try:
                path = await self.feishu.download_resource(msg.message_id, att.key, att.kind, dest, att.name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - FeishuError or anything from the SDK
                log.error("Download dell'allegato %s (%s) fallito: %s", att.name, att.key, exc)
                failed.append(att.name)
                continue
            paths.append(path)
            log.info("Chat %s: allegato %s scaricato in %s (%s)", msg.chat_id, att.name, path, _file_size(path))
        return paths, failed

    async def _report_failed_downloads(self, msg: IncomingMessage, failed: list[str]) -> None:
        names = ", ".join(f"«{n}»" for n in failed)
        await self.feishu.send_text(
            msg.chat_id, f"Non sono riuscito a scaricare {names}: puoi rimandarmelo?", reply_to=msg.message_id
        )

    # -- per-chat worker --------------------------------------------------------

    def _ensure_worker(self, chat: ChatState) -> None:
        task = self._workers.get(chat.chat_id)
        if task is None or task.done():
            self._workers[chat.chat_id] = asyncio.create_task(self._worker(chat), name=f"desk-worker-{chat.chat_id}")

    async def _worker(self, chat: ChatState) -> None:
        while True:
            msg = await chat.queue.get()
            try:
                await self._process(chat, msg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Errore nel worker della chat %s", chat.chat_id)
            finally:
                chat.queue.task_done()

    async def _process(self, chat: ChatState, msg: IncomingMessage) -> None:
        """One worker step: download attachments, then buffer as context or run the turn."""
        addressed = self._addressed.pop(msg.message_id, True)
        paths, failed = await self._download_attachments(msg)
        if not addressed:
            chat.append_context(msg, paths)
            log.info("Chat %s: messaggio %s salvato come contesto", chat.chat_id, msg.message_id)
            return
        if failed:
            await self._report_failed_downloads(msg, failed)
        if parse_command(msg.text) == "/reset":
            async with chat.lock:
                await self._reply_command(chat, msg, "/reset")
            return
        await self._run_addressed(chat, msg, paths)

    async def _run_addressed(self, chat: ChatState, msg: IncomingMessage, paths: list[Path]) -> None:
        """React, run the Claude turn (bounded per chat, globally and in time) and deliver."""
        working = await self.feishu.add_reaction(msg.message_id, REACTION_WORKING)
        async with self._semaphore, chat.lock:
            context = chat.drain_context()
            result = await self._run_turn(chat, msg, paths, context)
        await self._deliver(chat, msg, result)
        if working:
            await self.feishu.remove_reaction(msg.message_id, working)
        await self.feishu.add_reaction(msg.message_id, REACTION_DONE if result.error is None else REACTION_ERROR)
        log.info(
            "Chat %s: turno concluso (%s, %.1fs, costo %s)",
            chat.chat_id, "ok" if result.error is None else "errore", result.duration_ms / 1000,
            f"{result.cost_usd:.2f} USD" if result.cost_usd is not None else "n.d.",
        )

    async def _run_turn(
        self, chat: ChatState, msg: IncomingMessage, paths: list[Path], context: list[dict[str, Any]]
    ) -> TurnResult:
        """Run one turn with a wall-clock cap; a timeout cancels the query (the SDK ends the CLI)."""
        timeout = self.settings.turn_timeout_s
        start = time.monotonic()
        try:
            return await asyncio.wait_for(self.brain.run_turn(chat, msg, paths, context), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("Il turno per la chat %s ha superato %.0fs ed è stato interrotto", chat.chat_id, timeout)
            error = f"il turno ha superato {timeout:.0f}s ed è stato interrotto"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the brain should not raise, but never trust it
            log.exception("Il turno per la chat %s ha sollevato un'eccezione", chat.chat_id)
            error = f"errore inatteso: {exc}"
        duration_ms = int((time.monotonic() - start) * 1000)
        return TurnResult(text="", session_id=chat.session_id, cost_usd=None, error=error, duration_ms=duration_ms)

    async def _deliver(self, chat: ChatState, msg: IncomingMessage, result: TurnResult) -> None:
        text = (result.text or "").strip()
        interim = (result.last_interim_text or "").strip()
        if text and text != interim:
            await self.feishu.send_text(chat.chat_id, text, reply_to=msg.message_id)
        if result.error:
            await self.feishu.send_text(
                chat.chat_id, f"Scusa, non sono riuscito a completare la richiesta: {result.error}", reply_to=msg.message_id
            )
        elif not text and not interim and not result.sent_files:
            await self.feishu.send_text(
                chat.chat_id,
                "Ho finito, ma non ho nulla da aggiungere: se ti aspettavi un file o una risposta, dimmelo e riprovo.",
                reply_to=msg.message_id,
            )

    # -- slash commands --------------------------------------------------------

    async def _reply_command(self, chat: ChatState, msg: IncomingMessage, command: str) -> None:
        if command == "/reset":
            chat.reset()
            chat.drain_context()
            text = (
                "Fatto: ho azzerato la conversazione di questa chat. Dal prossimo messaggio riparto da zero; "
                "i documenti già creati restano disponibili."
            )
        elif command == "/status":
            text = self.status_text(chat)
        else:
            text = HELP_TEXT
        await self.feishu.send_text(chat.chat_id, text, reply_to=msg.message_id)

    def status_text(self, chat: ChatState) -> str:
        """Italian summary for ``/status``."""
        s = self.settings
        try:
            docs = self.store.list(chat.chat_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Elenco documenti non disponibile: %s", exc)
            docs = []
        session = f"attiva ({chat.session_id})" if chat.session_id else "nessuna: il prossimo messaggio ne apre una nuova"
        cost = f"{chat.last_cost_usd:.2f} USD" if chat.last_cost_usd is not None else "n.d."
        mode = "rispondo a tutti i messaggi del gruppo" if s.trigger_mode == "all" else "rispondo se menzionato o in privato"
        lines = [
            f"Sessione Claude: {session}",
            f"Documenti creati in questa chat: {len(docs)}",
            f"Costo dell'ultimo turno: {cost}",
            f"Richieste in coda: {chat.queue.qsize()}",
            f"Modello: {s.model} (effort {s.effort}) · {mode}",
        ]
        if docs:
            latest = docs[0]
            lines.append(f"Ultimo documento: {latest.get('title', latest.get('document_id'))} (v{latest.get('versions', '?')})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# --check and --render (used by run.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str


def check_python() -> Check:
    v = sys.version_info
    label = f"Python {v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 11):
        return Check("Python", "ok", label)
    return Check("Python", "fail", f"{label}: serve almeno Python 3.11")


def check_workspace(settings: Settings) -> Check:
    try:
        ws = ensure_workspace(Path(settings.workspace_dir))
        probe = ws / "logs" / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("Workspace", "fail", f"{settings.workspace_dir} non scrivibile: {exc}")
    return Check("Workspace", "ok", str(ws))


def check_brand(settings: Settings) -> Check:
    path = Path(settings.brand_path)
    if not path.is_file():
        return Check("Brand", "warn", f"{path} non trovato: uso i valori predefiniti (crea il file per personalizzare)")
    try:
        brand = load_brand(path)
    except (BrandError, ValueError, OSError) as exc:
        return Check("Brand", "fail", f"{path} non valido: {exc}")
    if not brand.logo_path.exists():
        return Check("Brand", "warn", f"{brand.company.name}: logo mancante ({brand.logo_path}); nell'intestazione comparirà il nome")
    return Check("Brand", "ok", f"{brand.company.name}, logo {brand.logo_path.name}, colori {brand.colors.primary}/{brand.colors.secondary}")


def check_soffice() -> Check:
    found = find_soffice()
    env = os.environ.get("WISEGLOW_SOFFICE", "").strip()
    note = f" (WISEGLOW_SOFFICE={env} non è un file valido: ignorato)" if env and env != found else ""
    if found:
        return Check("LibreOffice", "ok", found + note)
    return Check(
        "LibreOffice", "warn",
        "non trovato: i documenti usciranno solo in DOCX. Su Mac: brew install --cask libreoffice" + note,
    )


def check_anthropic_key(settings: Settings) -> Check:
    if settings.anthropic_api_key:
        return Check("ANTHROPIC_API_KEY", "ok", "impostata")
    return Check(
        "ANTHROPIC_API_KEY", "fail",
        "assente: crea una chiave su console.anthropic.com e mettila nel .env (l'accesso con abbonamento claude.ai non è consentito)",
    )


async def check_feishu(settings: Settings, feishu: FeishuLike | None = None) -> Check:
    client = feishu if feishu is not None else FeishuClient(
        settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_domain
    )
    try:
        open_id = await client.bot_open_id()
    except Exception as exc:  # noqa: BLE001
        return Check("Feishu", "fail", f"errore di rete o credenziali: {exc}")
    if not open_id:
        reason = getattr(client, "last_error", None)
        return Check(
            "Feishu", "fail",
            "impossibile leggere le informazioni del bot" + (f" ({reason})" if reason else "") + ": controlla "
            "FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_DOMAIN e che l'app abbia la capacità «Bot» pubblicata",
        )
    return Check("Feishu", "ok", f"bot «{client.bot_name or '?'}» ({open_id})")


async def run_checks(settings: Settings, *, feishu: FeishuLike | None = None) -> list[Check]:
    """All the ``--check`` diagnostics, in report order."""
    checks = [check_python(), check_workspace(settings), check_brand(settings), check_soffice(), check_anthropic_key(settings)]
    checks.append(await check_feishu(settings, feishu))
    return checks


def checks_ok(checks: list[Check]) -> bool:
    return all(c.status != "fail" for c in checks)


def format_report(checks: list[Check]) -> str:
    symbols = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = [f"  {symbols[c.status]} {c.name}: {c.detail}" for c in checks]
    verdict = "Tutto pronto." if checks_ok(checks) else "Ci sono problemi da risolvere prima di avviare il bot."
    return "Controllo WiseGlow Desk\n" + "\n".join(lines) + f"\n{verdict}"


@dataclass(frozen=True)
class RenderResult:
    docx_path: Path
    pdf_path: Path | None
    pages: int | None


def render_sample(json_path: Path, brand_path: Path, out_dir: Path, *, with_pdf: bool = True) -> RenderResult:
    """Render a Document JSON with the brand into ``out_dir`` (no Feishu, no store version)."""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    document = Document.from_dict(data)
    brand = load_brand(brand_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_path = render_docx(document, brand, out_dir / f"{document.document_id}.docx")
    pdf_path = docx_to_pdf(docx_path, out_dir) if with_pdf else None
    pages = pdf_page_count(pdf_path) if pdf_path else None
    return RenderResult(docx_path, pdf_path, pages)
