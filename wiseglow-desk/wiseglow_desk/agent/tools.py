"""In-process MCP tools for the WiseGlow agent (server ``wiseglow``).

The SDK ``@tool`` decorator wraps a static ``async def f(args) -> dict``; the
runtime (store, Feishu client, chat id, paths) is captured by closures built
in :func:`build_tools`, so each turn gets its own :class:`ToolContext`.

Every tool returns ``{"content": [{"type": "text", "text": ...}]}`` and, on
failure, adds ``"is_error": True`` with an Italian message.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from ..docgen.brand import BrandError, update_brand as brand_update
from ..docgen.catalog import catalog_markdown
from ..docgen.model import DOCUMENT_SCHEMA, Document, DocumentError

log = logging.getLogger(__name__)

SERVER_NAME = "wiseglow"
SERVER_VERSION = "1.0.0"
TOOL_NAMES: tuple[str, ...] = (
    "render_document",
    "get_document",
    "list_documents",
    "send_file",
    "send_message",
    "read_office_file",
    "save_reference",
    "update_brand",
    "catalog",
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
LOGO_FILE_NAME = "logo.png"  # where ``update_brand`` installs a new logo (spec §tools)


class ToolError(Exception):
    """A tool failure with an Italian message for the model."""


class FeishuLike(Protocol):
    """The subset of ``FeishuClient`` the tools use (so tests can pass fakes)."""

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None: ...

    async def send_file(self, chat_id: str, path: Path, *, reply_to: str | None = None) -> str | None: ...


class StoreLike(Protocol):
    """The subset of ``DocumentStore`` the tools use."""

    def save(self, document: Document, chat_id: str, created_by: str) -> Any: ...

    def latest(self, document_id: str) -> Any: ...

    def get_version(self, document_id: str, n: int) -> Any: ...

    def list(self, chat_id: str | None = None) -> list[dict[str, Any]]: ...


@dataclass
class ToolContext:
    """Runtime injected into the tools for one turn of one chat."""

    store: StoreLike
    brand_path: Path
    feishu: FeishuLike
    chat_id: str
    workspace: Path
    inbox_dir: Path
    knowledge_dir: Path
    reply_to_message_id: str | None = None
    created_by: str = ""
    # Set by ``send_message``: the bot compares the final reply against it.
    last_interim_text: str | None = None
    sent_files: list[Path] = field(default_factory=list)

    @property
    def brand_dir(self) -> Path:
        return Path(self.brand_path).resolve().parent

    @property
    def templates_dir(self) -> Path:
        return Path(self.knowledge_dir) / "templates"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _guard(fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Turn exceptions into Italian error results instead of SDK-formatted ones."""

    async def wrapper(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await fn(args or {})
        except ToolError as exc:
            return _err(f"Errore: {exc}")
        except Exception as exc:  # noqa: BLE001 - the model must see a readable message
            log.exception("Tool %s failed", fn.__name__)
            return _err(f"Errore inatteso in {fn.__name__}: {exc}")

    wrapper.__name__ = fn.__name__
    return wrapper


def safe_path(ctx: ToolContext, raw: str, roots: tuple[Path, ...] | None = None) -> Path:
    """Resolve ``raw`` and ensure it is inside one of ``roots`` (workspace + brand dir)."""
    if not raw or not str(raw).strip():
        raise ToolError("percorso mancante.")
    path = Path(str(raw).strip()).expanduser()
    if not path.is_absolute():
        path = Path(ctx.workspace) / path
    resolved = path.resolve()
    allowed = roots or (Path(ctx.workspace).resolve(), ctx.brand_dir)
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        raise ToolError(
            f"il percorso {resolved} è fuori dalle cartelle consentite "
            f"({', '.join(str(r) for r in allowed)})."
        )
    return resolved


def _existing_file(ctx: ToolContext, raw: str) -> Path:
    path = safe_path(ctx, raw)
    if not path.is_file():
        raise ToolError(f"il file {path} non esiste.")
    return path


def _size_label(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    return f"{size / 1024:.1f} KB" if size < 1_048_576 else f"{size / 1_048_576:.1f} MB"


def _safe_name(name: str, fallback: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", name.strip()).strip("._")
    return cleaned or fallback


# ---------------------------------------------------------------------------
# input schemas
# ---------------------------------------------------------------------------


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


# The document schema the model reads: "$schema" is meaningless inside a tool
# definition, so it is dropped (everything else is identical to DOCUMENT_SCHEMA).
DOCUMENT_TOOL_SCHEMA: dict[str, Any] = {k: v for k, v in DOCUMENT_SCHEMA.items() if k != "$schema"}

# The SDK validates arguments against the wire input_schema before the handler
# runs and reports only the first error, without a JSON path.  The full schema
# therefore travels in the tool description (for the model) while the wire
# property stays a loose object, so ``Document.from_dict`` gets to list every
# problem in Italian with its path (sections[1].blocks[1]: ...).
RENDER_DOCUMENT_SCHEMA: dict[str, Any] = _schema(
    {
        "document": {
            "type": "object",
            "description": "Il documento WiseGlow completo: segue lo schema JSON riportato nella descrizione dello strumento.",
        },
        "send": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": ["pdf", "docx"]},
            "default": ["pdf"],
            "description": "Formati da inviare subito in chat (default: solo PDF).",
        },
        "note": {
            "type": ["string", "null"],
            "default": "",
            "description": "Testo breve da inviare in chat prima dei file (opzionale).",
        },
    },
    ["document"],
)

RENDER_DOCUMENT_DESCRIPTION: str = (
    "Salva una nuova versione del documento (JSON completo), lo impagina con l'identità WiseGlow "
    "(DOCX + PDF) e invia subito in chat i formati richiesti. Unico modo consentito per produrre "
    "documenti. Stesso document_id = nuova versione.\n"
    "Schema JSON del campo `document` (campi obbligatori: document_id, doc_type, title, sections):\n"
    + json.dumps(DOCUMENT_TOOL_SCHEMA, ensure_ascii=False, separators=(",", ":"))
)


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def build_tools(ctx: ToolContext) -> McpSdkServerConfig:
    """Create the ``wiseglow`` MCP server whose tools close over ``ctx``."""
    return create_sdk_mcp_server(SERVER_NAME, SERVER_VERSION, tools=make_tools(ctx))


def make_tools(ctx: ToolContext) -> list[SdkMcpTool[Any]]:
    """The ``SdkMcpTool`` definitions (handlers close over ``ctx``)."""

    @tool("render_document", RENDER_DOCUMENT_DESCRIPTION, RENDER_DOCUMENT_SCHEMA)
    @_guard
    async def render_document(args: dict[str, Any]) -> dict[str, Any]:
        try:
            document = Document.from_dict(args.get("document"))
        except DocumentError as exc:
            return _err(str(exc))
        formats = [f for f in (args.get("send") or ["pdf"]) if f in ("pdf", "docx")] or ["pdf"]
        saved = await asyncio.to_thread(ctx.store.save, document, ctx.chat_id, ctx.created_by)
        note = (args.get("note") or "").strip()
        if note:
            await ctx.feishu.send_text(ctx.chat_id, note, reply_to=ctx.reply_to_message_id)
            ctx.last_interim_text = note
        lines = [
            f"Documento '{saved.document_id}' salvato come versione {saved.version}.",
            f"Pagine: {saved.pages if saved.pages is not None else 'n/d'}.",
            f"JSON: {saved.json_path}",
            f"DOCX: {saved.docx_path}",
            f"PDF: {saved.pdf_path}" if saved.pdf_path else "PDF non generato (LibreOffice assente).",
        ]
        for fmt in dict.fromkeys(formats):
            lines.append(await _send_format(ctx, fmt, saved))
        return _ok("\n".join(lines))

    @tool(
        "get_document",
        "Restituisce il JSON di un documento salvato (ultima versione, o quella indicata) "
        "così puoi modificarlo e ripassarlo a render_document.",
        _schema(
            {
                "document_id": {"type": "string"},
                "version": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "Numero di versione; omesso o null = ultima.",
                },
            },
            ["document_id"],
        ),
    )
    @_guard
    async def get_document(args: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(args.get("document_id") or "").strip()
        if not re.fullmatch(r"[a-z0-9-]{3,60}", doc_id):  # never a path: same slug rule as DOCUMENT_SCHEMA
            return _err("Errore: document_id non valido (slug di 3-60 caratteri: a-z, 0-9 e trattini).")
        version = args.get("version")
        found = ctx.store.get_version(doc_id, int(version)) if version else ctx.store.latest(doc_id)
        if found is None:
            return _err(f"Errore: documento '{doc_id}'" + (f" versione {version}" if version else "") + " non trovato.")
        document, saved = found
        header = f"document_id={saved.document_id} versione={saved.version} pdf={saved.pdf_path or 'n/d'}"
        return _ok(header + "\n" + json.dumps(document.to_dict(), ensure_ascii=False, indent=2))

    @tool(
        "list_documents",
        "Elenca i documenti salvati (di questa chat; all_chats=true per tutti).",
        _schema({"all_chats": {"type": ["boolean", "null"], "default": False}}),
    )
    @_guard
    async def list_documents(args: dict[str, Any]) -> dict[str, Any]:
        rows = ctx.store.list(None if args.get("all_chats") else ctx.chat_id)
        if not rows:
            return _ok("Nessun documento salvato" + ("" if args.get("all_chats") else " in questa chat") + ".")
        lines = [
            f"- {r.get('document_id')} — {r.get('doc_type')} — {r.get('title')} "
            f"(v{r.get('versions')}, aggiornato {str(r.get('updated', ''))[:16]})"
            for r in rows
        ]
        return _ok("\n".join(lines))

    @tool(
        "send_file",
        "Invia in chat un file già presente nel workspace (percorso assoluto).",
        _schema({"path": {"type": "string"}}, ["path"]),
    )
    @_guard
    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        path = _existing_file(ctx, str(args.get("path") or ""))
        message_id = await ctx.feishu.send_file(ctx.chat_id, path, reply_to=ctx.reply_to_message_id)
        if not message_id:
            return _err(f"Errore: invio di {path.name} fallito (Feishu non ha accettato il file).")
        ctx.sent_files.append(path)
        return _ok(f"File inviato: {path} ({_size_label(path)}).")

    @tool(
        "send_message",
        "Manda subito un messaggio in chat (avviso di lavoro lungo, risultato parziale). "
        "Il messaggio finale del turno viene inviato comunque: non ripeterlo qui.",
        _schema({"text": {"type": "string"}}, ["text"]),
    )
    @_guard
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "").strip()
        if not text:
            return _err("Errore: testo vuoto.")
        message_id = await ctx.feishu.send_text(ctx.chat_id, text, reply_to=ctx.reply_to_message_id)
        if not message_id:
            return _err("Errore: Feishu non ha accettato il messaggio.")
        ctx.last_interim_text = text
        return _ok("Messaggio inviato.")

    @tool(
        "read_office_file",
        "Estrae il testo da un file .docx/.xlsx/.csv/.txt/.md nel workspace. "
        "Per PDF e immagini usa invece lo strumento Read.",
        _schema({"path": {"type": "string"}}, ["path"]),
    )
    @_guard
    async def read_office_file(args: dict[str, Any]) -> dict[str, Any]:
        from ..docgen.office_text import UnsupportedFileError, extract_text

        path = _existing_file(ctx, str(args.get("path") or ""))
        try:
            text = await asyncio.to_thread(extract_text, path)
        except UnsupportedFileError as exc:
            return _err(f"Errore: {exc}")
        return _ok(text or "(file vuoto)")

    @tool(
        "save_reference",
        "Copia un file ricevuto (inbox) in knowledge/templates/ come riferimento riutilizzabile "
        "e lo annota in templates/index.md.",
        _schema(
            {
                "path": {"type": "string"},
                "name": {"type": "string", "description": "Nome breve del riferimento (es. loi-standard)."},
                "notes": {"type": ["string", "null"], "default": "", "description": "A cosa serve, cosa riusare."},
            },
            ["path", "name"],
        ),
    )
    @_guard
    async def save_reference(args: dict[str, Any]) -> dict[str, Any]:
        src = _existing_file(ctx, str(args.get("path") or ""))
        name = _safe_name(str(args.get("name") or ""), src.stem)
        notes = str(args.get("notes") or "").strip()
        dest = _copy_reference(ctx, src, name)
        _append_index(ctx, dest, src, notes)
        return _ok(f"Riferimento salvato: {dest}\nIndice: {ctx.templates_dir / 'index.md'}")

    @tool(
        "update_brand",
        "Aggiorna brand.yaml (solo su richiesta esplicita dell'utente). Chiavi note: company, colors, "
        "fonts, logo, page, footer, labels; 'logo_path' (file nel workspace) copia l'immagine in "
        "brand/logo.png e la imposta come logo. logo.file deve essere un nome di file nella cartella brand.",
        _schema({"patch": {"type": "object", "additionalProperties": True}}, ["patch"]),
    )
    @_guard
    async def update_brand(args: dict[str, Any]) -> dict[str, Any]:
        patch = dict(args.get("patch") or {})
        logo_raw = patch.pop("logo_path", None)
        logo_src = _logo_source(ctx, str(logo_raw)) if logo_raw else None
        if logo_src is not None:  # the renderer reads brand.logo.file: point it at the copy
            logo_patch = patch.get("logo") if isinstance(patch.get("logo"), dict) else {}
            patch["logo"] = {**logo_patch, "file": LOGO_FILE_NAME}
        _check_logo_file(ctx, patch)
        try:  # validate and write the patch first, then touch the logo file
            brand = brand_update(ctx.brand_path, patch)
        except BrandError as exc:
            return _err(f"Errore: {exc}")
        if logo_src is not None:
            _install_logo(ctx, logo_src)
        return _ok("Brand aggiornato.\n" + brand.summary())

    @tool(
        "catalog",
        "Il catalogo dei tipi di documento con le sezioni tipiche.",
        _schema({}),
    )
    @_guard
    async def catalog(args: dict[str, Any]) -> dict[str, Any]:
        return _ok(catalog_markdown())

    return [
        render_document,
        get_document,
        list_documents,
        send_file,
        send_message,
        read_office_file,
        save_reference,
        update_brand,
        catalog,
    ]


# ---------------------------------------------------------------------------
# tool internals
# ---------------------------------------------------------------------------


async def _send_format(ctx: ToolContext, fmt: str, saved: Any) -> str:
    """Send one rendered format to the chat and describe the outcome."""
    path: Path | None = saved.pdf_path if fmt == "pdf" else saved.docx_path
    if path is None or not Path(path).is_file():
        return f"Invio {fmt.upper()}: saltato, file non disponibile."
    message_id = await ctx.feishu.send_file(ctx.chat_id, Path(path), reply_to=ctx.reply_to_message_id)
    if not message_id:
        return f"Invio {fmt.upper()}: FALLITO (Feishu non ha accettato il file)."
    ctx.sent_files.append(Path(path))
    return f"Invio {fmt.upper()}: inviato in chat ({Path(path).name})."


def _copy_reference(ctx: ToolContext, src: Path, name: str) -> Path:
    ctx.templates_dir.mkdir(parents=True, exist_ok=True)
    dest = ctx.templates_dir / f"{name}{src.suffix.lower()}"
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    return dest


def _append_index(ctx: ToolContext, dest: Path, src: Path, notes: str) -> None:
    index = ctx.templates_dir / "index.md"
    if not index.exists():
        index.write_text("# Riferimenti e modelli salvati\n\n", encoding="utf-8")
    today = dt.date.today().isoformat()
    line = f"- `{dest.name}` — {notes or 'senza note'} (origine: {src.name}, salvato il {today})\n"
    with index.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _logo_source(ctx: ToolContext, raw: str) -> Path:
    """Validate a ``logo_path`` (inside the allowed folders, PNG/JPEG) without copying."""
    src = _existing_file(ctx, raw)
    if src.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ToolError("il logo deve essere un PNG o JPEG.")
    return src


def _check_logo_file(ctx: ToolContext, patch: dict[str, Any]) -> None:
    """``logo.file`` must name a file inside the brand folder (the renderer embeds it)."""
    logo = patch.get("logo")
    if not isinstance(logo, dict) or "file" not in logo:
        return
    name = str(logo["file"] or "").strip()
    bare = name and not Path(name).is_absolute() and "/" not in name and os.sep not in name and name not in (".", "..")
    if not bare or not (ctx.brand_dir / name).resolve().is_relative_to(ctx.brand_dir):
        raise ToolError(
            f"logo.file deve essere il nome di un file nella cartella brand ({ctx.brand_dir}), "
            f"non {name!r}; per usare un'immagine ricevuta passa 'logo_path'."
        )


def _install_logo(ctx: ToolContext, src: Path) -> Path:
    """Copy the validated logo source over ``brand/logo.png`` (no-op when it is that file)."""
    dest = ctx.brand_dir / LOGO_FILE_NAME
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest
