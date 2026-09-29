"""Tests for agent/tools.py: the in-process MCP tools with a fake Feishu client and the real store."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from claude_agent_sdk import SdkMcpTool  # noqa: E402

from wiseglow_desk.agent.tools import (  # noqa: E402
    RENDER_DOCUMENT_SCHEMA,
    SERVER_NAME,
    TOOL_NAMES,
    ToolContext,
    ToolError,
    build_tools,
    make_tools,
    safe_path,
)
from wiseglow_desk.docgen import store as store_mod  # noqa: E402
from wiseglow_desk.docgen.brand import Brand  # noqa: E402
from wiseglow_desk.docgen.model import DOCUMENT_SCHEMA  # noqa: E402
from wiseglow_desk.docgen.store import DocumentStore  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures and fakes
# ---------------------------------------------------------------------------


class FakeFeishu:
    """Records what the tools send; ``fail`` makes every send return None."""

    def __init__(self) -> None:
        self.texts: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.fail = False

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        self.texts.append({"chat_id": chat_id, "text": text, "reply_to": reply_to})
        return None if self.fail else f"om_t{len(self.texts)}"

    async def send_file(self, chat_id: str, path: Path, *, reply_to: str | None = None) -> str | None:
        self.files.append({"chat_id": chat_id, "path": Path(path), "reply_to": reply_to})
        return None if self.fail else f"om_f{len(self.files)}"


def sample_document(doc_id: str = "nbo-terreno-lecce") -> dict[str, Any]:
    return {
        "document_id": doc_id,
        "doc_type": "NBO",
        "language": "it",
        "title": "Offerta Non Vincolante",
        "subtitle": "Terreno agricolo – Comune di Lecce (LE), 12 ha",
        "reference": "WG-NBO-2026-014",
        "date": "2026-09-29",
        "place": "Milano",
        "confidential": True,
        "status": "Bozza",
        "recipient": {"company": "Azienda Agricola Rossi", "name": "Sig. Paolo Rossi"},
        "key_facts": [["Oggetto", "Acquisto terreno"], ["Superficie", "12 ha"]],
        "intro": "Egregio Sig. Rossi, facciamo seguito ai colloqui intercorsi.",
        "sections": [
            {"heading": "1. Oggetto", "blocks": [{"type": "paragraph", "text": "Acquisto del terreno **agricolo**."}]},
            {
                "heading": "2. Corrispettivo",
                "blocks": [{"type": "table", "columns": ["Voce", "Valore"], "rows": [["Prezzo", "[DA CONFERMARE]"]]}],
            },
        ],
        "closing": "Restiamo a disposizione.",
        "signatures": [{"party": "WiseGlow S.r.l.", "name": "Mario Rossi", "title": "Amministratore"}],
        "notes": "Documento non vincolante.",
    }


@pytest.fixture()
def fake_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace LibreOffice with a one-page PDF writer so saves are fast."""

    def _fake_docx_to_pdf(docx_path: Path, out_dir: Path, timeout: int = 120) -> Path:
        pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        with pdf.open("wb") as fh:
            writer.write(fh)
        return pdf

    monkeypatch.setattr(store_mod, "docx_to_pdf", _fake_docx_to_pdf)


@pytest.fixture()
def no_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "docx_to_pdf", lambda docx_path, out_dir, timeout=120: None)


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Any]:
    """A tmp workspace, a tmp copy of the brand, the real store, a fake Feishu and the tool handlers."""
    workspace = tmp_path / "workspace"
    brand_dir = tmp_path / "brand"
    brand_dir.mkdir()
    shutil.copy(ROOT / "brand" / "brand.yaml", brand_dir / "brand.yaml")
    if (ROOT / "brand" / "logo.png").exists():
        shutil.copy(ROOT / "brand" / "logo.png", brand_dir / "logo.png")
    inbox = workspace / "inbox" / "oc_group"
    inbox.mkdir(parents=True)
    feishu = FakeFeishu()
    ctx = ToolContext(
        store=DocumentStore(workspace / "documents", brand_path=brand_dir / "brand.yaml"),
        brand_path=brand_dir / "brand.yaml",
        feishu=feishu,
        chat_id="oc_group",
        workspace=workspace,
        inbox_dir=inbox,
        knowledge_dir=workspace / "knowledge",
        reply_to_message_id="om_user",
        created_by="Mario",
    )
    handlers = {t.name: t.handler for t in make_tools(ctx)}
    return {"ctx": ctx, "feishu": feishu, "tools": handlers, "workspace": workspace, "brand_dir": brand_dir, "inbox": inbox}


def call(env: dict[str, Any], tool_name: str, **args: Any) -> dict[str, Any]:
    return asyncio.run(env["tools"][tool_name](args))


def text_of(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# server + schemas
# ---------------------------------------------------------------------------


def test_build_tools_returns_sdk_server(env: dict[str, Any]) -> None:
    server = build_tools(env["ctx"])
    assert server["type"] == "sdk"
    assert server["name"] == SERVER_NAME == "wiseglow"
    assert server["instance"] is not None


def test_tool_names_and_schemas(env: dict[str, Any]) -> None:
    tools = make_tools(env["ctx"])
    assert all(isinstance(t, SdkMcpTool) for t in tools)
    assert tuple(t.name for t in tools) == TOOL_NAMES
    for t in tools:
        assert isinstance(t.input_schema, dict) and t.input_schema["type"] == "object"
        assert t.description  # Italian descriptions
    by_name = {t.name: t for t in tools}
    render = by_name["render_document"]
    # The wire schema keeps `document` a loose object (the SDK would otherwise stop at the first
    # error, without a path); the full schema is what the model reads in the description.
    assert render.input_schema["properties"]["document"]["type"] == "object"
    assert "properties" not in render.input_schema["properties"]["document"]
    full = {k: v for k, v in DOCUMENT_SCHEMA.items() if k != "$schema"}
    assert json.dumps(full, ensure_ascii=False, separators=(",", ":")) in render.description
    assert "$schema" not in render.description
    assert render.input_schema["required"] == ["document"]
    assert render.input_schema["properties"]["send"]["default"] == ["pdf"]


def test_render_schema_validates_inputs() -> None:
    jsonschema.validate({"document": sample_document()}, RENDER_DOCUMENT_SCHEMA)
    jsonschema.validate({"document": sample_document(), "send": ["pdf", "docx"], "note": "ecco"}, RENDER_DOCUMENT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"document": sample_document(), "send": ["html"]}, RENDER_DOCUMENT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"document": "non un oggetto"}, RENDER_DOCUMENT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"send": ["pdf"]}, RENDER_DOCUMENT_SCHEMA)
    # an incomplete document passes the wire check so the handler can list every problem
    jsonschema.validate({"document": {"title": "x"}}, RENDER_DOCUMENT_SCHEMA)


def test_optional_params_accept_null(env: dict[str, Any], fake_pdf: None) -> None:
    """A model that sends null for an optional field must reach the handler (SDK validates first)."""
    schemas = {t.name: t.input_schema for t in make_tools(env["ctx"])}
    jsonschema.validate({"document": sample_document(), "send": None, "note": None}, schemas["render_document"])
    jsonschema.validate({"document_id": "nbo-x", "version": None}, schemas["get_document"])
    jsonschema.validate({"all_chats": None}, schemas["list_documents"])
    jsonschema.validate({"path": "x", "name": "y", "notes": None}, schemas["save_reference"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"document_id": "nbo-x", "version": 0}, schemas["get_document"])

    res = call(env, "render_document", document=sample_document(), send=None, note=None)
    assert "is_error" not in res and "Invio PDF: inviato" in text_of(res)
    assert env["feishu"].texts == []
    latest = call(env, "get_document", document_id="nbo-terreno-lecce", version=None)
    assert "versione=1" in text_of(latest)
    assert "nbo-terreno-lecce" in text_of(call(env, "list_documents", all_chats=None))
    src = env["inbox"] / "modello.txt"
    src.write_text("x", encoding="utf-8")
    assert "is_error" not in call(env, "save_reference", path=str(src), name="modello", notes=None)


# ---------------------------------------------------------------------------
# render_document / get_document / list_documents
# ---------------------------------------------------------------------------


def test_render_document_saves_and_sends_pdf(env: dict[str, Any], fake_pdf: None) -> None:
    res = call(env, "render_document", document=sample_document(), note="Ecco la bozza della NBO.")
    assert "is_error" not in res
    text = text_of(res)
    assert "versione 1" in text and "Pagine: 1" in text
    assert "Invio PDF: inviato" in text

    feishu: FakeFeishu = env["feishu"]
    assert [t["text"] for t in feishu.texts] == ["Ecco la bozza della NBO."]
    assert feishu.texts[0]["reply_to"] == "om_user"
    assert len(feishu.files) == 1 and feishu.files[0]["path"].name == "v1.pdf"
    assert feishu.files[0]["chat_id"] == "oc_group"
    assert env["ctx"].last_interim_text == "Ecco la bozza della NBO."
    assert env["ctx"].sent_files == [feishu.files[0]["path"]]

    folder = env["workspace"] / "documents" / "nbo-terreno-lecce"
    assert (folder / "v1.json").is_file() and (folder / "v1.docx").is_file() and (folder / "v1.pdf").is_file()


def test_render_document_docx_and_versioning(env: dict[str, Any], fake_pdf: None) -> None:
    call(env, "render_document", document=sample_document())
    res = call(env, "render_document", document=sample_document(), send=["docx", "pdf"])
    text = text_of(res)
    assert "versione 2" in text
    assert "Invio DOCX: inviato" in text and "Invio PDF: inviato" in text
    names = [f["path"].name for f in env["feishu"].files]
    assert names == ["v1.pdf", "v2.docx", "v2.pdf"]


def test_render_document_without_libreoffice(env: dict[str, Any], no_pdf: None) -> None:
    res = call(env, "render_document", document=sample_document())
    text = text_of(res)
    assert "is_error" not in res
    assert "PDF non generato (LibreOffice assente)" in text
    assert "Invio PDF: saltato" in text
    assert env["feishu"].files == []


def test_render_document_reports_failed_send(env: dict[str, Any], fake_pdf: None) -> None:
    env["feishu"].fail = True
    res = call(env, "render_document", document=sample_document())
    assert "Invio PDF: FALLITO" in text_of(res)


def test_render_document_invalid_is_error(env: dict[str, Any], fake_pdf: None) -> None:
    bad = sample_document()
    bad["doc_type"] = "PIZZA"
    del bad["sections"]
    jsonschema.validate({"document": bad}, RENDER_DOCUMENT_SCHEMA)  # reaches the handler through the SDK
    res = call(env, "render_document", document=bad)
    assert res["is_error"] is True
    assert "Documento non valido (2 problemi)" in text_of(res)
    assert env["feishu"].files == [] and env["feishu"].texts == []


def test_render_document_lists_nested_problems_with_paths(env: dict[str, Any], fake_pdf: None) -> None:
    """Every problem, with its JSON path, in Italian: the model must not guess which block is wrong."""
    bad = sample_document()
    bad["sections"][1]["blocks"].append({"type": "bullets"})  # missing items
    bad["sections"][0]["blocks"][0]["style"] = "loud"  # style only valid on callouts, and not an enum value
    jsonschema.validate({"document": bad}, RENDER_DOCUMENT_SCHEMA)
    res = call(env, "render_document", document=bad)
    assert res["is_error"] is True
    text = text_of(res)
    assert "sections[1].blocks[1]" in text and "'items'" in text
    assert "sections[0].blocks[0].style" in text
    assert "Documento non valido (2 problemi)" in text

    assert call(env, "render_document", document="{}")["is_error"] is True


def test_get_document_latest_and_version(env: dict[str, Any], fake_pdf: None) -> None:
    call(env, "render_document", document=sample_document())
    second = sample_document()
    second["title"] = "Offerta Non Vincolante (rev. 2)"
    call(env, "render_document", document=second)

    latest = call(env, "get_document", document_id="nbo-terreno-lecce")
    assert "is_error" not in latest
    header, _, body = text_of(latest).partition("\n")
    assert "versione=2" in header
    data = json.loads(body)
    assert data["title"] == "Offerta Non Vincolante (rev. 2)"
    jsonschema.validate(data, DOCUMENT_SCHEMA)

    first = call(env, "get_document", document_id="nbo-terreno-lecce", version=1)
    assert json.loads(text_of(first).partition("\n")[2])["title"] == "Offerta Non Vincolante"

    missing = call(env, "get_document", document_id="non-esiste")
    assert missing["is_error"] is True and "non trovato" in text_of(missing)


def test_list_documents(env: dict[str, Any], fake_pdf: None) -> None:
    assert "Nessun documento" in text_of(call(env, "list_documents"))
    call(env, "render_document", document=sample_document())
    call(env, "render_document", document=sample_document("loi-parco-bari"))
    text = text_of(call(env, "list_documents"))
    assert "nbo-terreno-lecce" in text and "loi-parco-bari" in text and "NBO" in text
    # documents of other chats are hidden unless all_chats
    env["ctx"].chat_id = "oc_other"
    assert "Nessun documento" in text_of(call(env, "list_documents"))
    assert "nbo-terreno-lecce" in text_of(call(env, "list_documents", all_chats=True))


# ---------------------------------------------------------------------------
# send_file / send_message / path safety
# ---------------------------------------------------------------------------


def test_send_file_inside_workspace(env: dict[str, Any]) -> None:
    f = env["inbox"] / "planimetria.pdf"
    f.write_bytes(b"%PDF-1.4")
    res = call(env, "send_file", path=str(f))
    assert "is_error" not in res and "File inviato" in text_of(res)
    assert env["feishu"].files[0]["path"] == f.resolve()
    assert env["feishu"].files[0]["reply_to"] == "om_user"


def test_send_file_relative_path_and_brand_dir(env: dict[str, Any]) -> None:
    (env["workspace"] / "note.txt").write_text("x", encoding="utf-8")
    assert "is_error" not in call(env, "send_file", path="note.txt")
    assert "is_error" not in call(env, "send_file", path=str(env["brand_dir"] / "brand.yaml"))


def test_send_file_rejects_outside_paths(env: dict[str, Any], tmp_path: Path) -> None:
    outside = tmp_path / "segreto.txt"
    outside.write_text("no", encoding="utf-8")
    res = call(env, "send_file", path=str(outside))
    assert res["is_error"] is True and "fuori dalle cartelle consentite" in text_of(res)
    sneaky = call(env, "send_file", path=str(env["workspace"] / ".." / "segreto.txt"))
    assert sneaky["is_error"] is True
    assert env["feishu"].files == []


def test_send_file_missing_or_failed(env: dict[str, Any]) -> None:
    res = call(env, "send_file", path=str(env["workspace"] / "manca.pdf"))
    assert res["is_error"] is True and "non esiste" in text_of(res)
    f = env["workspace"] / "ok.pdf"
    f.write_bytes(b"x")
    env["feishu"].fail = True
    res = call(env, "send_file", path=str(f))
    assert res["is_error"] is True and "fallito" in text_of(res)


def test_send_message_records_interim(env: dict[str, Any]) -> None:
    res = call(env, "send_message", text="Sto leggendo i tre PDF, ci metto un minuto.")
    assert "is_error" not in res
    assert env["feishu"].texts[0]["text"] == "Sto leggendo i tre PDF, ci metto un minuto."
    assert env["ctx"].last_interim_text == "Sto leggendo i tre PDF, ci metto un minuto."
    assert call(env, "send_message", text="   ")["is_error"] is True
    env["feishu"].fail = True
    assert call(env, "send_message", text="x")["is_error"] is True


def test_safe_path_helper(env: dict[str, Any]) -> None:
    ctx: ToolContext = env["ctx"]
    assert safe_path(ctx, "inbox/oc_group") == (env["workspace"] / "inbox" / "oc_group").resolve()
    with pytest.raises(ToolError):
        safe_path(ctx, "/etc/passwd")
    with pytest.raises(ToolError):
        safe_path(ctx, "")


# ---------------------------------------------------------------------------
# read_office_file / save_reference / update_brand / catalog
# ---------------------------------------------------------------------------


def test_read_office_file_txt_and_docx(env: dict[str, Any], fake_pdf: None) -> None:
    txt = env["inbox"] / "appunti.txt"
    txt.write_text("Prezzo concordato: 45.000 euro/ha", encoding="utf-8")
    assert "45.000 euro/ha" in text_of(call(env, "read_office_file", path=str(txt)))

    call(env, "render_document", document=sample_document())
    docx = env["workspace"] / "documents" / "nbo-terreno-lecce" / "v1.docx"
    text = text_of(call(env, "read_office_file", path=str(docx)))
    assert "Offerta Non Vincolante" in text


def test_read_office_file_errors(env: dict[str, Any]) -> None:
    pdf = env["inbox"] / "x.pdf"
    pdf.write_bytes(b"%PDF")
    res = call(env, "read_office_file", path=str(pdf))
    assert res["is_error"] is True
    assert call(env, "read_office_file", path="/etc/hosts")["is_error"] is True


def test_save_reference_copies_and_indexes(env: dict[str, Any]) -> None:
    src = env["inbox"] / "20260929-101500_LOI_modello.docx"
    src.write_bytes(b"PK fake docx")
    res = call(env, "save_reference", path=str(src), name="LOI standard 2026", notes="clausole esclusiva e riservatezza")
    assert "is_error" not in res
    dest = env["workspace"] / "knowledge" / "templates" / "LOI_standard_2026.docx"
    assert dest.is_file() and dest.read_bytes() == b"PK fake docx"
    index = (env["workspace"] / "knowledge" / "templates" / "index.md").read_text(encoding="utf-8")
    assert "LOI_standard_2026.docx" in index and "clausole esclusiva" in index and src.name in index
    assert str(dest) in text_of(res)

    outside = env["workspace"].parent / "fuori.docx"
    outside.write_bytes(b"x")
    assert call(env, "save_reference", path=str(outside), name="x")["is_error"] is True


def test_update_brand_patch_and_logo(env: dict[str, Any]) -> None:
    logo = env["inbox"] / "nuovo_logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    res = call(
        env,
        "update_brand",
        patch={"colors": {"primary": "#123456"}, "company": {"phone": "+39 02 1234567"}, "logo_path": str(logo), "bogus": 1},
    )
    assert "is_error" not in res
    assert "Brand aggiornato" in text_of(res) and "#123456" in text_of(res)
    brand = Brand.from_yaml(env["brand_dir"] / "brand.yaml")
    assert brand.colors.primary == "#123456"
    assert brand.company.phone == "+39 02 1234567"
    assert brand.company.name == "WiseGlow S.r.l."  # untouched keys survive
    assert (env["brand_dir"] / "logo.png").read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    assert brand.logo.file == "logo.png" and brand.logo_path == (env["brand_dir"] / "logo.png").resolve()


def test_update_brand_errors(env: dict[str, Any]) -> None:
    res = call(env, "update_brand", patch={"colors": {"primary": "blu"}})
    assert res["is_error"] is True and "Colore non valido" in text_of(res)
    res = call(env, "update_brand", patch={"logo_path": "/etc/hosts"})
    assert res["is_error"] is True


def test_update_brand_validates_before_installing_logo(env: dict[str, Any]) -> None:
    """An invalid patch must not leave a half-applied change (the logo swapped, the colour rejected)."""
    before = (env["brand_dir"] / "logo.png").read_bytes()
    logo = env["inbox"] / "nuovo.png"
    logo.write_bytes(b"\x89PNG new")
    res = call(env, "update_brand", patch={"logo_path": str(logo), "colors": {"primary": "blu"}})
    assert res["is_error"] is True and "Colore non valido" in text_of(res)
    assert (env["brand_dir"] / "logo.png").read_bytes() == before
    assert Brand.from_yaml(env["brand_dir"] / "brand.yaml").colors.primary == "#243F58"


def test_update_brand_rejects_logo_file_outside_brand_dir(env: dict[str, Any], tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "private.png"
    outside.parent.mkdir()
    outside.write_bytes(b"\x89PNG private")
    original = (env["brand_dir"] / "brand.yaml").read_text(encoding="utf-8")
    for bad in (str(outside), "../elsewhere/private.png", "/etc/hosts", "sub/logo.png", "..", ""):
        res = call(env, "update_brand", patch={"logo": {"file": bad}})
        assert res["is_error"] is True and "logo.file" in text_of(res), bad
    assert (env["brand_dir"] / "brand.yaml").read_text(encoding="utf-8") == original
    # a bare file name inside the brand folder is fine
    (env["brand_dir"] / "marchio.png").write_bytes(b"\x89PNG m")
    res = call(env, "update_brand", patch={"logo": {"file": "marchio.png"}})
    assert "is_error" not in res
    assert Brand.from_yaml(env["brand_dir"] / "brand.yaml").logo_path == (env["brand_dir"] / "marchio.png").resolve()


def test_update_brand_logo_path_is_what_the_renderer_uses(env: dict[str, Any]) -> None:
    """After a rename of logo.file, logo_path must still end up as the logo the renderer embeds."""
    (env["brand_dir"] / "wiseglow.png").write_bytes(b"\x89PNG old")
    assert "is_error" not in call(env, "update_brand", patch={"logo": {"file": "wiseglow.png"}})
    logo = env["inbox"] / "nuovo.png"
    logo.write_bytes(b"\x89PNG new")
    res = call(env, "update_brand", patch={"logo_path": str(logo)})
    assert "is_error" not in res and "presente" in text_of(res)
    brand = Brand.from_yaml(env["brand_dir"] / "brand.yaml")
    assert brand.logo_path == (env["brand_dir"] / "logo.png").resolve()
    assert brand.logo_path.read_bytes() == b"\x89PNG new"
    # logo_path together with a contradictory logo.file: the installed copy wins
    res = call(env, "update_brand", patch={"logo_path": str(logo), "logo": {"file": "marchio.png", "width_cm": 3.5}})
    assert "is_error" not in res
    brand = Brand.from_yaml(env["brand_dir"] / "brand.yaml")
    assert brand.logo.file == "logo.png" and brand.logo.width_cm == 3.5 and brand.logo_path.exists()


def test_update_brand_logo_path_same_file(env: dict[str, Any]) -> None:
    """Pointing logo_path at brand/logo.png itself (what the prompt prints) must not crash."""
    current = env["brand_dir"] / "logo.png"
    before = current.read_bytes()
    res = call(env, "update_brand", patch={"logo_path": str(current), "colors": {"primary": "#123456"}})
    assert "is_error" not in res, text_of(res)
    assert current.read_bytes() == before
    assert Brand.from_yaml(env["brand_dir"] / "brand.yaml").colors.primary == "#123456"


def test_catalog_tool(env: dict[str, Any]) -> None:
    text = text_of(call(env, "catalog"))
    assert "`NBO`" in text and "`LOI`" in text and "`INTERESSE_TERRENO`" in text
    assert "Natura non vincolante" in text


def test_unexpected_exception_becomes_italian_error(env: dict[str, Any]) -> None:
    class BrokenStore:
        def list(self, chat_id: str | None = None) -> list[dict[str, Any]]:
            raise RuntimeError("disco rotto")

    env["ctx"].store = BrokenStore()
    res = call(env, "list_documents")
    assert res["is_error"] is True and "Errore inatteso" in text_of(res) and "disco rotto" in text_of(res)


def test_get_document_rejects_path_like_ids(env: dict[str, Any], fake_pdf: None) -> None:
    call(env, "render_document", document=sample_document())
    for bad in ("../nbo-terreno-lecce", "/etc", "NBO-Terreno", "ab"):
        res = call(env, "get_document", document_id=bad)
        assert res["is_error"] is True and "document_id non valido" in text_of(res)
