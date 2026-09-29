"""Versioned on-disk store for generated documents.

Layout (``root`` is the ``documents`` folder of the workspace)::

    <root>/<document_id>/v<N>.json   the Document JSON (DOCUMENT_SCHEMA shape)
    <root>/<document_id>/v<N>.docx   rendered with the brand
    <root>/<document_id>/v<N>.pdf    LibreOffice conversion (absent if soffice missing)
    <root>/index.json                {id: {doc_type, title, chat_id, versions, updated, latest_pdf, latest_docx, ...}}

Saving the same ``document_id`` again creates the next version.  A single
``threading.Lock`` serialises version numbering, rendering and index updates.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .brand import Brand, load_brand
from .docx_renderer import render_docx
from .model import Document
from .pdf import docx_to_pdf, pdf_page_count

log = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^v(\d+)\.json$")
_SLUG_RE = re.compile(r"[a-z0-9-]{3,60}")  # same rule as DOCUMENT_SCHEMA's document_id
_DEFAULT_BRAND_PATH = Path(__file__).resolve().parents[2] / "brand" / "brand.yaml"


def is_valid_document_id(document_id: str) -> bool:
    """True when ``document_id`` is a slug (so it can never escape the store root)."""
    return bool(_SLUG_RE.fullmatch(str(document_id)))


@dataclass(frozen=True)
class SavedVersion:
    document_id: str
    version: int
    json_path: Path
    docx_path: Path
    pdf_path: Path | None
    pages: int | None


class DocumentStore:
    """Versioned document store rooted at the workspace ``documents`` folder.

    ``root`` may be either the ``documents`` folder itself or the workspace
    folder (in which case ``<root>/documents`` is used).  ``brand_path``
    defaults to the repository's ``brand/brand.yaml``; it is re-read at every
    save so brand edits apply immediately.
    """

    def __init__(self, root: Path | str, brand_path: Path | str | None = None) -> None:
        root = Path(root).resolve()
        self.root: Path = root if root.name == "documents" else root / "documents"
        self.brand_path: Path = Path(brand_path).resolve() if brand_path else _DEFAULT_BRAND_PATH
        self.index_path: Path = self.root / "index.json"
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- write ---------------------------------------------------------------

    def save(self, document: Document, chat_id: str, created_by: str) -> SavedVersion:
        """Persist a new version of ``document`` and render DOCX + PDF."""
        with self._lock:
            folder = self.root / document.document_id
            folder.mkdir(parents=True, exist_ok=True)
            version = self._next_version(folder)
            # render first, persist last: a v<N>.json exists only for versions that have a DOCX
            docx_path = render_docx(document, self._brand(), folder / f"v{version}.docx")
            pdf_path = docx_to_pdf(docx_path, folder)
            pages = _safe_page_count(pdf_path)
            json_path = folder / f"v{version}.json"
            _write_json(json_path, document.to_dict())
            saved = SavedVersion(document.document_id, version, json_path, docx_path, pdf_path, pages)
            self._update_index(document, saved, chat_id, created_by)
            return saved

    # -- read ----------------------------------------------------------------

    def versions(self, document_id: str) -> list[int]:
        """Sorted version numbers available for ``document_id`` (empty for a non-slug id)."""
        if not is_valid_document_id(document_id):
            return []
        folder = self.root / document_id
        if not folder.is_dir():
            return []
        found = [int(m.group(1)) for p in folder.iterdir() if (m := _VERSION_RE.match(p.name))]
        return sorted(found)

    def get_version(self, document_id: str, n: int) -> tuple[Document, SavedVersion] | None:
        if not is_valid_document_id(document_id):
            return None
        folder = self.root / document_id
        json_path = folder / f"v{n}.json"
        if not json_path.is_file():
            return None
        document = Document.from_dict(json.loads(json_path.read_text(encoding="utf-8")))
        docx_path = folder / f"v{n}.docx"
        pdf_path = folder / f"v{n}.pdf"
        pdf = pdf_path if pdf_path.is_file() else None
        return document, SavedVersion(document_id, n, json_path, docx_path, pdf, _safe_page_count(pdf))

    def latest(self, document_id: str) -> tuple[Document, SavedVersion] | None:
        versions = self.versions(document_id)
        return self.get_version(document_id, versions[-1]) if versions else None

    def list(self, chat_id: str | None = None) -> list[dict[str, Any]]:
        """Index entries (plus ``document_id``), newest first, optionally filtered by chat."""
        index = self._read_index()
        rows = [{"document_id": doc_id, **entry} for doc_id, entry in index.items()]
        if chat_id is not None:
            rows = [r for r in rows if r.get("chat_id") == chat_id]
        # newest first; ties (same timestamp) broken by most recent insertion
        order = {id(r): i for i, r in enumerate(rows)}
        rows.sort(key=lambda r: (str(r.get("updated", "")), order[id(r)]), reverse=True)
        return rows

    # -- internals -----------------------------------------------------------

    def _brand(self) -> Brand:
        return load_brand(self.brand_path)

    def _next_version(self, folder: Path) -> int:
        versions = self.versions(folder.name)
        return (versions[-1] + 1) if versions else 1

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("index.json illeggibile (%s): lo ricreo", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _update_index(self, document: Document, saved: SavedVersion, chat_id: str, created_by: str) -> None:
        index = self._read_index()
        index[document.document_id] = {
            "doc_type": document.doc_type,
            "title": document.title,
            "chat_id": chat_id,
            "versions": saved.version,
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "created_by": created_by,
            "latest_pdf": str(saved.pdf_path) if saved.pdf_path else None,
            "latest_docx": str(saved.docx_path),
            "pages": saved.pages,
        }
        _write_json(self.index_path, index)


def _safe_page_count(pdf_path: Path | None) -> int | None:
    if pdf_path is None or not pdf_path.is_file():
        return None
    try:
        return pdf_page_count(pdf_path)
    except Exception as exc:  # corrupt PDF must not break a save
        log.warning("Conteggio pagine fallito per %s: %s", pdf_path, exc)
        return None


def _write_json(path: Path, data: Any) -> None:
    """Atomic JSON write (temp file + replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
