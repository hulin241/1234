"""Tests for the document model, schema validation and the catalog."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import jsonschema
import pytest

from wiseglow_desk.docgen.catalog import DOC_TYPES, catalog_markdown
from wiseglow_desk.docgen.model import DOCUMENT_SCHEMA, Document, DocumentError

MINIMAL: dict[str, Any] = {
    "document_id": "test-doc",
    "doc_type": "GENERIC",
    "title": "Prova",
    "sections": [{"heading": "Uno", "blocks": [{"type": "paragraph", "text": "ciao"}]}],
}


def test_schema_is_valid_draft7() -> None:
    jsonschema.Draft7Validator.check_schema(DOCUMENT_SCHEMA)
    assert DOCUMENT_SCHEMA["additionalProperties"] is False
    assert DOCUMENT_SCHEMA["required"] == ["document_id", "doc_type", "title", "sections"]


@pytest.mark.parametrize("name", ["nbo", "loi", "interesse_terreno"])
def test_samples_are_valid(sample_dicts: dict[str, dict[str, Any]], name: str) -> None:
    doc = Document.from_dict(sample_dicts[name])
    assert doc.document_id == sample_dicts[name]["document_id"]
    assert len(doc.sections) >= 3
    assert doc.doc_type in DOC_TYPES


@pytest.mark.parametrize("name", ["nbo", "loi", "interesse_terreno"])
def test_round_trip(sample_dicts: dict[str, dict[str, Any]], name: str) -> None:
    doc = Document.from_dict(sample_dicts[name])
    again = Document.from_dict(doc.to_dict())
    assert again == doc
    assert Document.validate_dict(doc.to_dict()) == []


def test_defaults_are_filled() -> None:
    doc = Document.from_dict(MINIMAL)
    assert doc.language == "it"
    assert doc.date == dt.date.today().isoformat()
    assert doc.sections[0].level == 1
    assert doc.confidential is False
    assert doc.parties == [] and doc.signatures == [] and doc.attachments == []
    assert doc.recipient is None
    assert doc.doc_type_name == "Documento generico"


def test_missing_required_fields_reported_in_italian() -> None:
    with pytest.raises(DocumentError) as exc:
        Document.from_dict({"title": "x"})
    msg = str(exc.value)
    assert "Documento non valido" in msg
    assert "'document_id'" in msg and "'doc_type'" in msg and "'sections'" in msg
    assert "manca il campo obbligatorio" in msg


def test_every_problem_is_listed_with_its_path() -> None:
    bad = copy.deepcopy(MINIMAL)
    bad["document_id"] = "Non Valido!"
    bad["extra"] = 1
    bad["sections"][0]["blocks"].append({"type": "table", "columns": ["a"]})
    bad["sections"][0]["blocks"].append({"type": "bullets"})
    bad["recipient"] = {"phone": "123"}
    problems = Document.validate_dict(bad)
    joined = "\n".join(problems)
    assert len(problems) >= 5
    assert "document_id: deve essere uno slug" in joined
    assert "campo non previsto 'extra'" in joined
    assert "sections[0].blocks[1]: manca il campo obbligatorio 'rows'" in joined
    assert "sections[0].blocks[2]: manca il campo obbligatorio 'items'" in joined
    assert "recipient: campo non previsto 'phone'" in joined
    with pytest.raises(DocumentError) as exc:
        Document.from_dict(bad)
    assert f"({len(problems)} problemi)" in str(exc.value)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "paragraph"},
        {"type": "callout"},
        {"type": "quote"},
        {"type": "numbered"},
        {"type": "table", "rows": []},
        {"type": "unknown", "text": "x"},
        {"type": "callout", "text": "x", "style": "danger"},
    ],
)
def test_block_requirements(block: dict[str, Any]) -> None:
    bad = copy.deepcopy(MINIMAL)
    bad["sections"][0]["blocks"] = [block]
    problems = Document.validate_dict(bad)
    assert problems and all(p.startswith("sections[0].blocks[0]") for p in problems)


def test_invalid_values() -> None:
    bad = copy.deepcopy(MINIMAL)
    bad["doc_type"] = "FOO"
    bad["language"] = "de"
    bad["date"] = "29/09/2026"
    bad["sections"] = []
    bad["key_facts"] = [["solo-uno"]]
    problems = Document.validate_dict(bad)
    joined = "\n".join(problems)
    assert "doc_type: valore non ammesso 'FOO'" in joined
    assert "language: valore non ammesso 'de'" in joined
    assert "date: formato non valido" in joined
    assert "sections: servono almeno 1 elementi" in joined
    assert "key_facts[0]: servono almeno 2 elementi" in joined


def test_date_must_be_real() -> None:
    bad = dict(MINIMAL, date="2026-13-40")
    assert Document.validate_dict(bad) == ["date: non è una data valida (usa YYYY-MM-DD)"]


def test_numeric_cells_are_accepted_and_stringified() -> None:
    data = copy.deepcopy(MINIMAL)
    data["key_facts"] = [["Potenza", 5.5]]
    data["sections"][0]["blocks"] = [{"type": "table", "columns": ["Voce", "Valore"], "rows": [["kWp", 1200]], "col_widths": [30, 70]}]
    doc = Document.from_dict(data)
    assert doc.key_facts == [("Potenza", "5.5")]
    assert doc.sections[0].blocks[0].rows == [["kWp", "1200"]]
    assert doc.sections[0].blocks[0].col_widths == [30.0, 70.0]


def test_to_dict_omits_empty_optionals() -> None:
    doc = Document.from_dict(MINIMAL)
    d = doc.to_dict()
    assert set(d) == {"document_id", "doc_type", "language", "title", "date", "sections"}
    assert d["sections"][0] == {"heading": "Uno", "level": 1, "blocks": [{"type": "paragraph", "text": "ciao"}]}


def test_brand_yaml_round_trip_and_paths(brand_path: Any, tmp_path: Any) -> None:
    from wiseglow_desk.docgen.brand import Brand, BrandError, load_brand, update_brand

    brand = Brand.from_yaml(brand_path)
    assert brand.company.name == "WiseGlow S.r.l." and brand.colors.primary == "#243F58"
    assert brand.fonts.body_cjk == "PingFang SC" and brand.page.margins_cm.top == 2.3
    assert brand.logo_path == (brand_path.parent / "logo.png").resolve() and brand.logo_path.is_file()
    assert brand.labels_for("zh")["page"] == "第" and brand.labels_for("xx")["page"] == "Pagina"
    assert "WiseGlow" in brand.summary()

    copy_path = tmp_path / "b" / "brand.yaml"
    brand.to_yaml(copy_path)
    again = Brand.from_yaml(copy_path)
    assert again.to_dict() == brand.to_dict()

    updated = update_brand(copy_path, {"colors": {"primary": "0a0b0c"}, "fonts": {"unknown": 1}, "labels": {"en": {"date": "Dated", "bogus": "x"}}, "nope": 1})
    assert updated.colors.primary == "#0A0B0C" and updated.colors.secondary == "#FF670D"
    assert updated.labels["en"]["date"] == "Dated" and "bogus" not in updated.labels["en"]
    assert Brand.from_yaml(copy_path).colors.primary == "#0A0B0C"

    with pytest.raises(BrandError, match="Colore non valido"):
        Brand.from_dict({"colors": {"primary": "blu"}})
    assert load_brand(tmp_path / "missing.yaml").company.name == "WiseGlow S.r.l."


def test_catalog_contents() -> None:
    assert "NBO" in DOC_TYPES and "GENERIC" in DOC_TYPES and len(DOC_TYPES) == 15
    assert DOC_TYPES["NBO"].skeleton[0] == "Premessa"
    assert DOC_TYPES["GENERIC"].skeleton == []
    assert DOC_TYPES["LOI"].name_zh == "意向书"
    md = catalog_markdown()
    for code in DOC_TYPES:
        assert f"`{code}`" in md
    assert "Sezioni tipiche" in md
    assert set(DOCUMENT_SCHEMA["properties"]["doc_type"]["enum"]) == set(DOC_TYPES)


def test_brand_yaml_tolerates_unknown_keys_and_blanks(brand_path: Any, tmp_path: Any, caplog: Any) -> None:
    """A hand-edited brand.yaml with extra keys or blank values must still load (it is re-read at every render)."""
    import logging
    import re

    from wiseglow_desk.docgen.brand import Brand, BrandError, load_brand

    text = brand_path.read_text(encoding="utf-8")
    edits = [
        ('  phone: ""\n', '  phone: ""\n  pec: "wiseglow@pec.it"\n'),
        ('  callout_bg: "#E1F0F6"', '  callout_bg: "#E1F0F6"\n  accent: "#000000"'),
        ("margins_cm: { top: 2.3,", "margins_cm: { gutter: 1, top: null,"),
        ("  width_cm: 4.6\n", "  width_cm: null\n"),
    ]
    for old, new in edits:
        assert old in text
        text = text.replace(old, new)
    text = re.sub(r"^  zh: .*$", "  zh:", text, flags=re.M)  # a language left blank
    path = tmp_path / "brand.yaml"
    path.write_text(text, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="wiseglow_desk.docgen.brand"):
        brand = load_brand(path)
    assert brand.company.phone == "" and brand.colors.primary == "#243F58"
    assert brand.page.margins_cm.top == 2.3 and brand.logo.width_cm == 4.6
    assert brand.labels_for("zh")["page"] == "第"  # blank language keeps the default labels
    for key in ("company.pec", "colors.accent", "page.margins_cm.gutter", "labels.zh"):
        assert key in caplog.text

    with pytest.raises(BrandError, match="brand.yaml non valido"):
        Brand.from_dict({"logo": {"width_cm": "larga"}})
    with pytest.raises(BrandError, match="brand.yaml non valido"):
        Brand.from_dict({"company": "WiseGlow"})


def test_missing_fields_are_listed_once_each() -> None:
    problems = Document.validate_dict({"x": 1})
    required = [p for p in problems if "manca il campo obbligatorio" in p]
    assert len(required) == 4 and len(set(required)) == 4
    assert "(radice): manca il campo obbligatorio 'document_id'" in required
    assert len(problems) == 5  # 4 missing + 1 unknown field, matching the header count
    with pytest.raises(DocumentError, match=r"\(5 problemi\)"):
        Document.from_dict({"x": 1})
