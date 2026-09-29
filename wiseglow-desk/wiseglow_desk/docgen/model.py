"""Document model: JSON schema, dataclasses and validation.

``DOCUMENT_SCHEMA`` is the single contract shared by the MCP tool input, the
on-disk ``v<N>.json`` files and the renderer.  ``Document.from_dict`` validates
a dict against it with :mod:`jsonschema` and raises :class:`DocumentError`
with an Italian message that lists every problem.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .catalog import DOC_TYPES

LANGUAGES: tuple[str, ...] = ("it", "en", "zh")
BLOCK_TYPES: tuple[str, ...] = (
    "paragraph",
    "bullets",
    "numbered",
    "table",
    "callout",
    "quote",
    "page_break",
)
CALLOUT_STYLES: tuple[str, ...] = ("info", "warning", "note")

_CELL = {"type": ["string", "number"]}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

_BLOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type"],
    "properties": {
        "type": {"type": "string", "enum": list(BLOCK_TYPES)},
        "text": {"type": "string"},
        "items": _STR_LIST,
        "columns": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "array", "items": _CELL}},
        "col_widths": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}},
        "caption": {"type": "string"},
        "style": {"type": "string", "enum": list(CALLOUT_STYLES)},
    },
    "allOf": [
        {
            "if": {"required": ["type"], "properties": {"type": {"enum": ["paragraph", "callout", "quote"]}}},
            "then": {"required": ["text"]},
        },
        {
            "if": {"required": ["type"], "properties": {"type": {"enum": ["bullets", "numbered"]}}},
            "then": {"required": ["items"]},
        },
        {
            "if": {"required": ["type"], "properties": {"type": {"const": "table"}}},
            "then": {"required": ["columns", "rows"]},
        },
    ],
}

DOCUMENT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "WiseGlow document",
    "type": "object",
    "additionalProperties": False,
    "required": ["document_id", "doc_type", "title", "sections"],
    "properties": {
        "document_id": {
            "type": "string",
            "pattern": "^[a-z0-9-]{3,60}$",
            "description": "Slug stabile del documento; lo stesso id crea una nuova versione.",
        },
        "doc_type": {"type": "string", "enum": list(DOC_TYPES.keys())},
        "language": {"type": "string", "enum": list(LANGUAGES), "default": "it"},
        "title": {"type": "string", "minLength": 1},
        "subtitle": {"type": "string"},
        "reference": {"type": "string"},
        "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "Data ISO (YYYY-MM-DD)."},
        "place": {"type": "string"},
        "confidential": {"type": "boolean"},
        "status": {"type": "string"},
        "recipient": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "company": {"type": "string"},
                "name": {"type": "string"},
                "role": {"type": "string"},
                "address": {"type": "string"},
                "email": {"type": "string"},
            },
        },
        "parties": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["role", "name"],
                "properties": {
                    "role": {"type": "string"},
                    "name": {"type": "string"},
                    "details": {"type": "string"},
                },
            },
        },
        "key_facts": {
            "type": "array",
            "items": {"type": "array", "minItems": 2, "maxItems": 2, "items": _CELL},
        },
        "intro": {"type": "string"},
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading"],
                "properties": {
                    "heading": {"type": "string", "minLength": 1},
                    "level": {"type": "integer", "enum": [1, 2], "default": 1},
                    "blocks": {"type": "array", "items": _BLOCK_SCHEMA},
                },
            },
        },
        "closing": {"type": "string"},
        "signatures": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["party"],
                "properties": {
                    "party": {"type": "string"},
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
        },
        "attachments": _STR_LIST,
        "notes": {"type": "string"},
    },
}


class DocumentError(ValueError):
    """Raised when a document dict does not match ``DOCUMENT_SCHEMA``."""


# ----------------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------------


@dataclass
class Recipient:
    company: str | None = None
    name: str | None = None
    role: str | None = None
    address: str | None = None
    email: str | None = None

    def lines(self) -> list[str]:
        """Non-empty address lines in display order."""
        return [v for v in (self.company, self.name, self.role, self.address, self.email) if v]


@dataclass
class Party:
    role: str
    name: str
    details: str | None = None


@dataclass
class Block:
    type: str
    text: str | None = None
    items: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    col_widths: list[float] | None = None
    caption: str | None = None
    style: str = "info"


@dataclass
class Section:
    heading: str
    level: int = 1
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Signature:
    party: str
    name: str = ""
    title: str = ""


@dataclass
class Document:
    document_id: str
    doc_type: str
    title: str
    sections: list[Section]
    language: str = "it"
    subtitle: str | None = None
    reference: str | None = None
    date: str = ""
    place: str | None = None
    confidential: bool = False
    status: str | None = None
    recipient: Recipient | None = None
    parties: list[Party] = field(default_factory=list)
    key_facts: list[tuple[str, str]] = field(default_factory=list)
    intro: str | None = None
    closing: str | None = None
    signatures: list[Signature] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    notes: str | None = None

    # -- construction --------------------------------------------------------

    @classmethod
    def validate_dict(cls, data: Any) -> list[str]:
        """Return the list of Italian problem descriptions (empty = valid)."""
        validator = jsonschema.Draft7Validator(DOCUMENT_SCHEMA)
        errors = sorted(validator.iter_errors(data), key=lambda e: e.json_path)
        problems = [_describe(e) for e in errors]
        if not problems and isinstance(data, dict) and data.get("date"):
            try:
                dt.date.fromisoformat(data["date"])
            except ValueError:
                problems.append("date: non è una data valida (usa YYYY-MM-DD)")
        return problems

    @classmethod
    def from_dict(cls, data: Any) -> "Document":
        """Validate ``data`` and build a Document; raise DocumentError otherwise."""
        problems = cls.validate_dict(data)
        if problems:
            n = len(problems)
            head = "Documento non valido (1 problema):" if n == 1 else f"Documento non valido ({n} problemi):"
            raise DocumentError(head + "\n" + "\n".join(f"- {p}" for p in problems))
        recipient = data.get("recipient")
        return cls(
            document_id=data["document_id"],
            doc_type=data["doc_type"],
            title=data["title"],
            sections=[_section_from(s) for s in data["sections"]],
            language=data.get("language") or "it",
            subtitle=data.get("subtitle"),
            reference=data.get("reference"),
            date=data.get("date") or dt.date.today().isoformat(),
            place=data.get("place"),
            confidential=bool(data.get("confidential", False)),
            status=data.get("status"),
            recipient=Recipient(**recipient) if recipient else None,
            parties=[Party(**p) for p in data.get("parties", [])],
            key_facts=[(str(k), str(v)) for k, v in data.get("key_facts", [])],
            intro=data.get("intro"),
            closing=data.get("closing"),
            signatures=[Signature(**s) for s in data.get("signatures", [])],
            attachments=list(data.get("attachments", [])),
            notes=data.get("notes"),
        )

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Schema-shaped dict; optional empty fields are omitted."""
        out: dict[str, Any] = {
            "document_id": self.document_id,
            "doc_type": self.doc_type,
            "language": self.language,
            "title": self.title,
        }
        _put(out, "subtitle", self.subtitle)
        _put(out, "reference", self.reference)
        _put(out, "date", self.date)
        _put(out, "place", self.place)
        if self.confidential:
            out["confidential"] = True
        _put(out, "status", self.status)
        if self.recipient:
            rec = {k: v for k, v in vars(self.recipient).items() if v}
            if rec:
                out["recipient"] = rec
        if self.parties:
            out["parties"] = [_compact(vars(p)) for p in self.parties]
        if self.key_facts:
            out["key_facts"] = [[k, v] for k, v in self.key_facts]
        _put(out, "intro", self.intro)
        out["sections"] = [_section_to(s) for s in self.sections]
        _put(out, "closing", self.closing)
        if self.signatures:
            out["signatures"] = [
                {"party": s.party, "name": s.name, "title": s.title} for s in self.signatures
            ]
        if self.attachments:
            out["attachments"] = list(self.attachments)
        _put(out, "notes", self.notes)
        return out

    # -- convenience ---------------------------------------------------------

    @property
    def date_value(self) -> dt.date:
        """The document date as a ``date`` (today if unset)."""
        try:
            return dt.date.fromisoformat(self.date)
        except ValueError:
            return dt.date.today()

    @property
    def doc_type_name(self) -> str:
        """Italian catalog name of the doc type (the code if unknown)."""
        entry = DOC_TYPES.get(self.doc_type)
        return entry.name_it if entry else self.doc_type


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _put(out: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        out[key] = value


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "")}


def _section_from(s: dict[str, Any]) -> Section:
    return Section(
        heading=s["heading"],
        level=int(s.get("level") or 1),
        blocks=[_block_from(b) for b in s.get("blocks", [])],
    )


def _block_from(b: dict[str, Any]) -> Block:
    return Block(
        type=b["type"],
        text=b.get("text"),
        items=[str(i) for i in b.get("items", [])],
        columns=[str(c) for c in b.get("columns", [])],
        rows=[[str(c) for c in row] for row in b.get("rows", [])],
        col_widths=[float(w) for w in b["col_widths"]] if b.get("col_widths") else None,
        caption=b.get("caption"),
        style=b.get("style") or "info",
    )


def _section_to(s: Section) -> dict[str, Any]:
    out: dict[str, Any] = {"heading": s.heading, "level": s.level}
    if s.blocks:
        out["blocks"] = [_block_to(b) for b in s.blocks]
    return out


def _block_to(b: Block) -> dict[str, Any]:
    out: dict[str, Any] = {"type": b.type}
    if b.type in ("paragraph", "callout", "quote"):
        out["text"] = b.text or ""
    if b.type == "callout":
        out["style"] = b.style
    if b.type in ("bullets", "numbered"):
        out["items"] = list(b.items)
    if b.type == "table":
        out["columns"] = list(b.columns)
        out["rows"] = [list(r) for r in b.rows]
        if b.col_widths:
            out["col_widths"] = list(b.col_widths)
        _put(out, "caption", b.caption)
    return out


def _path_of(error: jsonschema.ValidationError) -> str:
    parts: list[str] = []
    for p in error.absolute_path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            parts.append(("." if parts else "") + str(p))
    return "".join(parts) or "(radice)"


def _describe(error: jsonschema.ValidationError) -> str:
    """Translate one jsonschema error into an Italian one-liner."""
    v = error.validator
    path = _path_of(error)
    if v == "required":
        # jsonschema yields one error per missing property: name only that one
        m = re.match(r"^'(.+)' is a required property$", error.message)
        name = f"'{m.group(1)}'" if m else error.message
        return f"{path}: manca il campo obbligatorio {name}"
    if v == "additionalProperties":
        extra = sorted(set(error.instance) - set(error.schema.get("properties", {})))
        names = ", ".join(f"'{e}'" for e in extra)
        return f"{path}: campo non previsto {names}"
    if v == "type":
        types = error.validator_value
        types = types if isinstance(types, list) else [types]
        return f"{path}: deve essere di tipo {' o '.join(_TYPE_IT.get(t, t) for t in types)}"
    if v in ("enum", "const"):
        allowed = error.validator_value if v == "enum" else [error.validator_value]
        return f"{path}: valore non ammesso {error.instance!r} (ammessi: {', '.join(map(str, allowed))})"
    if v == "pattern":
        if path == "document_id":
            return f"{path}: deve essere uno slug di 3-60 caratteri con solo a-z, 0-9 e trattini"
        return f"{path}: formato non valido ({error.instance!r})"
    if v == "minItems":
        return f"{path}: servono almeno {error.validator_value} elementi"
    if v == "maxItems":
        return f"{path}: al massimo {error.validator_value} elementi"
    if v == "minLength":
        return f"{path}: non può essere vuoto"
    if v == "exclusiveMinimum":
        return f"{path}: deve essere maggiore di {error.validator_value}"
    return f"{path}: {error.message}"


_TYPE_IT = {
    "string": "testo",
    "number": "numero",
    "integer": "intero",
    "boolean": "booleano",
    "array": "lista",
    "object": "oggetto",
}
