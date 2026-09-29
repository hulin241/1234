"""End-to-end simulation: Feishu event -> Desk -> brain -> real render_document -> replies.

No network and no Claude: a real ``P2ImMessageReceiveV1`` payload enters the
``Desk``; a ``FakeFeishu`` records what the bot sends; a ``ToolCallingBrain``
stands in for Claude and drives the *real* ``wiseglow`` tool handlers
(``read_office_file`` on the attachment, ``render_document`` for the PDF).
When LibreOffice is missing or broken the PDF step is stubbed out and the
DOCX is delivered instead.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: E402
from lark_oapi.core.json import JSON  # noqa: E402

from wiseglow_desk.agent.brain import TurnResult  # noqa: E402
from wiseglow_desk.agent.sessions import ChatState, SessionRegistry  # noqa: E402
from wiseglow_desk.agent.tools import ToolContext, make_tools  # noqa: E402
from wiseglow_desk.bot import REACTION_DONE, REACTION_WORKING, Desk  # noqa: E402
from wiseglow_desk.config import Settings  # noqa: E402
from wiseglow_desk.docgen import store as store_mod  # noqa: E402
from wiseglow_desk.docgen.brand import load_brand  # noqa: E402
from wiseglow_desk.docgen.pdf import pdf_page_count  # noqa: E402
from wiseglow_desk.docgen.store import DocumentStore  # noqa: E402
from wiseglow_desk.feishu.events import IncomingMessage  # noqa: E402

BOT_OPEN_ID = "ou_bot_0000000000000000000000000000"
USER_OPEN_ID = "ou_user_111111111111111111111111111"
CHAT_ID = "oc_e2e_group"
FILE_KEY = "file_v2_e2e_visura"
DOC_ID = "nbo-e2e-terreno"
FINAL_REPLY = "Ti ho mandato la NBO in PDF: dimmi pure cosa cambiare."


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeFeishu:
    """Records sends/reactions; ``download_resource`` copies a file from disk."""

    def __init__(self, source_file: Path) -> None:
        self.source_file = source_file
        self.sent_message_ids: deque[str] = deque(maxlen=500)
        self.bot_name: str | None = "WiseGlow Desk"
        self.texts: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.reactions: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        message_id = f"om_bot_{self._counter}"
        self.sent_message_ids.append(message_id)
        return message_id

    async def bot_open_id(self) -> str | None:
        return BOT_OPEN_ID

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        self.texts.append({"chat_id": chat_id, "text": text, "reply_to": reply_to})
        return self._next_id()

    async def send_file(self, chat_id: str, path: Path, *, reply_to: str | None = None) -> str | None:
        assert Path(path).is_file(), f"file inesistente inviato: {path}"
        self.files.append({"chat_id": chat_id, "path": Path(path), "reply_to": reply_to})
        return self._next_id()

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str | None:
        self.reactions.append((message_id, emoji_type))
        return f"reaction-{len(self.reactions)}"

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        self.removed.append((message_id, reaction_id))

    async def download_resource(self, message_id: str, key: str, kind: str, dest_dir: Path, name_hint: str) -> Path:
        assert key == FILE_KEY and kind == "file"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"20260929-120000_{name_hint}"
        shutil.copy(self.source_file, dest)
        return dest


class ToolCallingBrain:
    """Stands in for Claude: reads the attachment and renders a document with the real tools."""

    def __init__(self, settings: Settings, store: DocumentStore, feishu: FakeFeishu) -> None:
        self.settings = settings
        self.store = store
        self.feishu = feishu
        self.calls: list[dict[str, Any]] = []
        self.tool_results: list[str] = []

    def _context(self, chat: ChatState, msg: IncomingMessage) -> ToolContext:
        workspace = Path(self.settings.workspace_dir)
        return ToolContext(
            store=self.store,
            brand_path=Path(self.settings.brand_path),
            feishu=self.feishu,
            chat_id=chat.chat_id,
            workspace=workspace,
            inbox_dir=workspace / "inbox" / chat.chat_id,
            knowledge_dir=workspace / "knowledge",
            reply_to_message_id=msg.message_id,
            created_by=msg.sender_open_id,
        )

    async def run_turn(
        self, chat: ChatState, msg: IncomingMessage, attachment_paths: list[Path], context: list[dict[str, Any]]
    ) -> TurnResult:
        self.calls.append({"msg": msg, "paths": list(attachment_paths), "context": list(context)})
        ctx = self._context(chat, msg)
        tools = {t.name: t for t in make_tools(ctx)}

        # The file arrived in an earlier (unaddressed) message: its path is in the buffered context.
        material = next(p for entry in context for p in entry.get("attachments", []))
        read = await tools["read_office_file"].handler({"path": material})
        assert not read.get("is_error"), read
        surface_line = next(line for line in read["content"][0]["text"].splitlines() if "Superficie" in line)

        document = _sample_document(surface_line)
        rendered = await tools["render_document"].handler(
            {"document": document, "send": ["pdf", "docx"], "note": "Ecco la bozza della NBO."}
        )
        text = rendered["content"][0]["text"]
        self.tool_results.append(text)
        assert not rendered.get("is_error"), text

        chat.session_id = "sess-e2e"
        chat.save()
        chat.last_cost_usd = 0.42
        return TurnResult(
            text=FINAL_REPLY,
            session_id=chat.session_id,
            cost_usd=0.42,
            error=None,
            duration_ms=5,
            last_interim_text=ctx.last_interim_text,
            sent_files=list(ctx.sent_files),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sample_document(surface_line: str) -> dict[str, Any]:
    return {
        "document_id": DOC_ID,
        "doc_type": "NBO",
        "language": "it",
        "title": "Offerta Non Vincolante",
        "subtitle": "Terreno agricolo – Comune di Lecce (LE)",
        "reference": "WG-NBO-2026-099",
        "date": "2026-09-29",
        "place": "Milano",
        "confidential": True,
        "status": "Bozza",
        "recipient": {"company": "Azienda Agricola Rossi", "name": "Sig. Paolo Rossi"},
        "key_facts": [["Oggetto", "Acquisto terreno"], ["Superficie", surface_line.split(":", 1)[-1].strip()]],
        "sections": [
            {"heading": "1. Oggetto", "blocks": [{"type": "paragraph", "text": "Acquisto del terreno **agricolo**."}]},
            {
                "heading": "2. Corrispettivo",
                "blocks": [
                    {"type": "table", "columns": ["Voce", "Valore"], "rows": [["Prezzo", "[DA CONFERMARE: prezzo]"]]},
                    {"type": "bullets", "items": ["Caparra alla firma del preliminare.", "Saldo al rogito."]},
                ],
            },
        ],
        "closing": "Restiamo a disposizione.",
        "signatures": [{"party": "WiseGlow S.r.l.", "name": "[DA CONFERMARE: firmatario]", "title": "Amministratore"}],
        "notes": "Bozza non vincolante.",
    }


def _event(
    message_id: str,
    message_type: str,
    content: dict[str, Any],
    *,
    mentions: list[dict[str, Any]] | None = None,
) -> P2ImMessageReceiveV1:
    """A real SDK event object built from an ``im.message.receive_v1`` payload."""
    message: dict[str, Any] = {
        "message_id": message_id,
        "create_time": "1727614800123",
        "update_time": "1727614800123",
        "chat_id": CHAT_ID,
        "chat_type": "group",
        "message_type": message_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    if mentions is not None:
        message["mentions"] = mentions
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": f"ev_{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": "1727614800200",
            "token": "",
            "app_id": "cli_test",
            "tenant_key": "2ca1d211f64f1234",
        },
        "event": {
            "sender": {
                "sender_id": {"union_id": "on_e2e", "user_id": "e2euser", "open_id": USER_OPEN_ID},
                "sender_type": "user",
                "tenant_key": "2ca1d211f64f1234",
            },
            "message": message,
        },
    }
    return JSON.unmarshal(json.dumps(payload, ensure_ascii=False), P2ImMessageReceiveV1)


def _bot_mention(key: str = "@_user_1") -> dict[str, Any]:
    return {
        "key": key,
        "id": {"union_id": "on_bot", "user_id": "", "open_id": BOT_OPEN_ID},
        "name": "WiseGlow Desk",
        "tenant_key": "2ca1d211f64f1234",
    }


# ---------------------------------------------------------------------------
# the simulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_simulation(tmp_path: Path, pdf_available: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    if not pdf_available:  # no LibreOffice here: the PDF step is skipped, the DOCX still goes out
        monkeypatch.setattr(store_mod, "docx_to_pdf", lambda docx_path, out_dir, timeout=120: None)

    material = tmp_path / "visura.txt"
    material.write_text("Comune: Lecce\nFoglio: 187\nSuperficie: 12,10 ha\n", encoding="utf-8")

    settings = Settings(
        feishu_app_id="cli_test",
        feishu_app_secret="x",
        anthropic_api_key="sk-ant-test",
        workspace_dir=tmp_path / "workspace",
        brand_path=ROOT / "brand" / "brand.yaml",
    )
    feishu = FakeFeishu(material)
    store = DocumentStore(settings.workspace_dir / "documents", brand_path=settings.brand_path)
    brain = ToolCallingBrain(settings, store, feishu)
    desk = Desk(
        settings,
        feishu=feishu,
        brain=brain,
        registry=SessionRegistry(settings.workspace_dir),
        store=store,
        brand=load_brand(settings.brand_path),
    )
    await desk._resolve_identity()
    assert desk.bot_open_id == BOT_OPEN_ID

    # 1) the user drops a file in the group without mentioning the bot: downloaded and buffered
    file_event = _event("om_file_1", "file", {"file_key": FILE_KEY, "file_name": "visura.txt"})
    assert await desk.handle_event(file_event) == "context"
    await desk.drain()  # the download and the buffering run in the per-chat worker
    inbox_files = list((settings.workspace_dir / "inbox" / CHAT_ID).iterdir())
    assert len(inbox_files) == 1 and inbox_files[0].name.endswith("_visura.txt")
    assert feishu.reactions == []  # unaddressed: no reaction, no reply

    # 2) then asks for the document with an @mention
    text_event = _event(
        "om_text_2",
        "text",
        {"text": "@_user_1 mi prepari la NBO per il terreno della visura che ho mandato?"},
        mentions=[_bot_mention()],
    )
    assert await desk.handle_event(text_event) == "queued"
    await asyncio.wait_for(desk.drain(), timeout=60)

    # the brain saw the mention, the cleaned text and the buffered attachment path
    assert len(brain.calls) == 1
    call = brain.calls[0]
    assert call["msg"].mentions_bot and call["msg"].text.startswith("mi prepari la NBO")
    assert call["context"] and call["context"][0]["attachments"] == [str(inbox_files[0])]

    # the document was saved as version 1 with the brand and rendered
    found = store.latest(DOC_ID)
    assert found is not None
    document, saved = found
    assert saved.version == 1 and saved.docx_path.is_file()
    assert document.key_facts[1] == ("Superficie", "12,10 ha")
    assert store.list(CHAT_ID)[0]["document_id"] == DOC_ID

    # the files were "sent" to the chat as replies to the user's message
    sent = {f["path"].suffix: f for f in feishu.files}
    assert ".docx" in sent and sent[".docx"]["reply_to"] == "om_text_2"
    if pdf_available:
        assert ".pdf" in sent and sent[".pdf"]["reply_to"] == "om_text_2"
        assert saved.pdf_path is not None and 1 <= pdf_page_count(saved.pdf_path) <= 4
        assert "Invio PDF: inviato" in brain.tool_results[0]
    else:
        assert ".pdf" not in sent
        assert "PDF non generato (LibreOffice assente)" in brain.tool_results[0]

    # the note went out before the files, then the final reply; all as replies in the same chat
    texts = [t["text"] for t in feishu.texts]
    assert texts == ["Ecco la bozza della NBO.", FINAL_REPLY]
    assert all(t["chat_id"] == CHAT_ID and t["reply_to"] == "om_text_2" for t in feishu.texts)
    assert len(feishu.sent_message_ids) == len(feishu.texts) + len(feishu.files)

    # reactions: OnIt while working (then removed) and DONE at the end
    assert feishu.reactions[0] == ("om_text_2", REACTION_WORKING)
    assert feishu.reactions[-1] == ("om_text_2", REACTION_DONE)
    assert feishu.removed == [("om_text_2", "reaction-1")]

    # session persisted for the next turn
    session = json.loads((settings.workspace_dir / "chats" / CHAT_ID / "session.json").read_text(encoding="utf-8"))
    assert session["session_id"] == "sess-e2e"

    # and /status now reports it without involving the brain
    status_event = _event("om_text_3", "text", {"text": "@_user_1 /status"}, mentions=[_bot_mention()])
    assert await desk.handle_event(status_event) == "command"
    assert "sess-e2e" in feishu.texts[-1]["text"] and "Documenti creati in questa chat: 1" in feishu.texts[-1]["text"]
    assert len(brain.calls) == 1
