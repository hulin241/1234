"""Document generation for WiseGlow Desk.

The package turns a validated ``Document`` (see :mod:`model`) into a branded
DOCX (:mod:`docx_renderer`) and PDF (:mod:`pdf`), keeps versioned copies on
disk (:mod:`store`) and offers helpers to read office files sent by users
(:mod:`office_text`).  The visual identity lives in :mod:`brand` + the
renderer only: content is free, presentation is always the same.
"""

from .brand import Brand, load_brand, save_brand, update_brand
from .catalog import DOC_TYPES, DocType, catalog_markdown
from .model import DOCUMENT_SCHEMA, Document, DocumentError
from .store import DocumentStore, SavedVersion

__all__ = [
    "Brand",
    "load_brand",
    "save_brand",
    "update_brand",
    "DOC_TYPES",
    "DocType",
    "catalog_markdown",
    "DOCUMENT_SCHEMA",
    "Document",
    "DocumentError",
    "DocumentStore",
    "SavedVersion",
]
