"""Tests for the versioned DocumentStore."""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from wiseglow_desk.docgen import store as store_mod
from wiseglow_desk.docgen.brand import update_brand
from wiseglow_desk.docgen.model import Document
from wiseglow_desk.docgen.store import DocumentStore, SavedVersion

MINIMAL: dict[str, Any] = {
    "document_id": "store-doc",
    "doc_type": "NBO",
    "title": "Offerta di prova",
    "date": "2026-09-29",
    "sections": [{"heading": "Uno", "blocks": [{"type": "paragraph", "text": "ciao"}]}],
}


@pytest.fixture
def no_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip LibreOffice: conversion returns None (fast tests)."""
    monkeypatch.setattr(store_mod, "docx_to_pdf", lambda docx, out_dir, timeout=120: None)


@pytest.fixture
def fake_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace LibreOffice with a one-page blank PDF writer."""

    def _fake(docx: Path, out_dir: Path, timeout: int = 120) -> Path:
        pdf = Path(out_dir) / (Path(docx).stem + ".pdf")
        w = PdfWriter()
        w.add_blank_page(width=595, height=842)
        with open(pdf, "wb") as fh:
            w.write(fh)
        return pdf

    monkeypatch.setattr(store_mod, "docx_to_pdf", _fake)


def test_save_creates_versions_and_index(tmp_workspace: Path, brand_path: Path, no_pdf: None) -> None:
    st = DocumentStore(tmp_workspace / "documents", brand_path=brand_path)
    doc = Document.from_dict(MINIMAL)
    v1 = st.save(doc, chat_id="oc_1", created_by="ou_alice")
    assert isinstance(v1, SavedVersion)
    assert v1.version == 1 and v1.pdf_path is None and v1.pages is None
    assert v1.json_path == tmp_workspace / "documents" / "store-doc" / "v1.json"
    assert v1.docx_path.is_file() and v1.json_path.is_file()

    doc2 = Document.from_dict(dict(MINIMAL, title="Offerta di prova v2"))
    v2 = st.save(doc2, chat_id="oc_1", created_by="ou_bob")
    assert v2.version == 2 and v2.docx_path.name == "v2.docx"

    index = json.loads(st.index_path.read_text(encoding="utf-8"))
    entry = index["store-doc"]
    assert entry["versions"] == 2 and entry["title"] == "Offerta di prova v2"
    assert entry["doc_type"] == "NBO" and entry["chat_id"] == "oc_1"
    assert entry["latest_docx"].endswith("v2.docx") and entry["latest_pdf"] is None
    assert entry["created_by"] == "ou_bob" and entry["updated"]


def test_latest_get_version_and_list(tmp_workspace: Path, brand_path: Path, no_pdf: None) -> None:
    st = DocumentStore(tmp_workspace / "documents", brand_path=brand_path)
    st.save(Document.from_dict(MINIMAL), "oc_1", "u")
    st.save(Document.from_dict(dict(MINIMAL, title="Seconda")), "oc_1", "u")
    other = dict(MINIMAL, document_id="altro-doc", title="Altro")
    st.save(Document.from_dict(other), "oc_2", "u")

    assert st.versions("store-doc") == [1, 2]
    assert st.versions("inesistente") == []
    assert st.latest("inesistente") is None
    assert st.get_version("store-doc", 9) is None

    doc, saved = st.latest("store-doc")  # type: ignore[misc]
    assert doc.title == "Seconda" and saved.version == 2
    doc1, saved1 = st.get_version("store-doc", 1)  # type: ignore[misc]
    assert doc1.title == "Offerta di prova" and saved1.json_path.name == "v1.json"

    rows = st.list()
    assert {r["document_id"] for r in rows} == {"store-doc", "altro-doc"}
    assert rows[0]["document_id"] == "altro-doc"  # newest first
    assert [r["document_id"] for r in st.list(chat_id="oc_1")] == ["store-doc"]
    assert st.list(chat_id="oc_none") == []


def test_root_accepts_workspace_or_documents_dir(tmp_workspace: Path, brand_path: Path, no_pdf: None) -> None:
    a = DocumentStore(tmp_workspace, brand_path=brand_path)
    b = DocumentStore(tmp_workspace / "documents", brand_path=brand_path)
    assert a.root == b.root == (tmp_workspace / "documents").resolve()
    a.save(Document.from_dict(MINIMAL), "oc", "u")
    assert b.latest("store-doc") is not None


def test_pdf_pages_recorded(tmp_workspace: Path, brand_path: Path, fake_pdf: None) -> None:
    st = DocumentStore(tmp_workspace / "documents", brand_path=brand_path)
    saved = st.save(Document.from_dict(MINIMAL), "oc", "u")
    assert saved.pdf_path is not None and saved.pdf_path.is_file() and saved.pages == 1
    _, again = st.latest("store-doc")  # type: ignore[misc]
    assert again.pdf_path == saved.pdf_path and again.pages == 1
    entry = json.loads(st.index_path.read_text(encoding="utf-8"))["store-doc"]
    assert entry["latest_pdf"].endswith("v1.pdf") and entry["pages"] == 1


def test_brand_is_reloaded_at_every_save(tmp_workspace: Path, brand_path: Path, no_pdf: None) -> None:
    my_brand = tmp_workspace / "brand" / "brand.yaml"
    my_brand.parent.mkdir()
    my_brand.write_text(brand_path.read_text(encoding="utf-8"), encoding="utf-8")
    st = DocumentStore(tmp_workspace, brand_path=my_brand)
    st.save(Document.from_dict(MINIMAL), "oc", "u")
    update_brand(my_brand, {"company": {"name": "Nuova Ragione Sociale S.p.A."}, "ignota": 1})
    saved = st.save(Document.from_dict(MINIMAL), "oc", "u")
    from docx import Document as DocxDocument

    assert DocxDocument(str(saved.docx_path)).core_properties.author == "Nuova Ragione Sociale S.p.A."


def test_concurrent_saves_get_distinct_versions(tmp_workspace: Path, brand_path: Path, no_pdf: None) -> None:
    st = DocumentStore(tmp_workspace, brand_path=brand_path)
    results: list[int] = []

    def worker(i: int) -> None:
        d = copy.deepcopy(MINIMAL)
        d["title"] = f"Titolo {i}"
        results.append(st.save(Document.from_dict(d), "oc", "u").version)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [1, 2, 3, 4]
    assert st.versions("store-doc") == [1, 2, 3, 4]


def test_real_pdf_conversion(require_pdf: None, tmp_workspace: Path, brand_path: Path, sample_dicts: dict[str, Any]) -> None:
    st = DocumentStore(tmp_workspace, brand_path=brand_path)
    saved = st.save(Document.from_dict(sample_dicts["interesse_terreno"]), "oc", "u")
    assert saved.pdf_path is not None and saved.pdf_path.is_file()
    assert saved.pages is not None and saved.pages >= 1


def test_failed_render_leaves_no_phantom_version(tmp_workspace: Path, brand_path: Path, no_pdf: None) -> None:
    """A version JSON must exist only when its DOCX was rendered (e.g. a broken brand.yaml)."""
    from wiseglow_desk.docgen.brand import BrandError

    good = brand_path.read_text(encoding="utf-8")
    my_brand = tmp_workspace / "brand.yaml"
    my_brand.write_text(good.replace('primary: "#243F58"', 'primary: "blu scuro"'), encoding="utf-8")
    st = DocumentStore(tmp_workspace, brand_path=my_brand)
    with pytest.raises(BrandError):
        st.save(Document.from_dict(MINIMAL), "oc", "u")
    assert st.versions("store-doc") == [] and st.latest("store-doc") is None
    assert not list((st.root / "store-doc").glob("v*")) and not st.index_path.exists()

    my_brand.write_text(good, encoding="utf-8")
    saved = st.save(Document.from_dict(MINIMAL), "oc", "u")
    assert saved.version == 1 and saved.docx_path.is_file() and saved.json_path.is_file()
    assert json.loads(st.index_path.read_text(encoding="utf-8"))["store-doc"]["versions"] == 1


def test_reads_reject_non_slug_ids(tmp_workspace: Path, brand_path: Path, no_pdf: None, tmp_path: Path) -> None:
    st = DocumentStore(tmp_workspace, brand_path=brand_path)
    st.save(Document.from_dict(MINIMAL), "oc", "u")
    other = DocumentStore(tmp_path / "other", brand_path=brand_path)
    escaped = os.path.relpath(st.root / "store-doc", other.root)
    assert escaped.startswith("..")
    assert other.versions(escaped) == [] and other.latest(escaped) is None
    assert other.get_version(str(st.root / "store-doc"), 1) is None
    for bad in ("", "ab", "Store-Doc", "a/b", "..."):
        assert st.versions(bad) == [] and st.latest(bad) is None
    assert st.versions("store-doc") == [1]
