"""Shared pytest fixtures: repo paths, brand, samples, temp workspace, PDF availability."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wiseglow_desk.docgen.brand import Brand  # noqa: E402
from wiseglow_desk.docgen.pdf import docx_to_pdf, find_soffice  # noqa: E402

SAMPLE_NAMES = ("nbo", "loi", "interesse_terreno")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def brand_path(repo_root: Path) -> Path:
    return repo_root / "brand" / "brand.yaml"


@pytest.fixture(scope="session")
def brand(brand_path: Path) -> Brand:
    return Brand.from_yaml(brand_path)


@pytest.fixture(scope="session")
def samples_dir(repo_root: Path) -> Path:
    return repo_root / "samples"


@pytest.fixture(scope="session")
def sample_dicts(samples_dir: Path) -> dict[str, dict[str, Any]]:
    return {name: json.loads((samples_dir / f"{name}.json").read_text(encoding="utf-8")) for name in SAMPLE_NAMES}


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture(scope="session")
def pdf_available(tmp_path_factory: pytest.TempPathFactory) -> bool:
    """True when LibreOffice is present AND can actually convert a document.

    A ``soffice`` binary without the Writer component (e.g. libreoffice-core
    only) exists but fails every conversion; tests needing a real PDF skip.
    """
    if find_soffice() is None:
        return False
    from docx import Document

    folder = tmp_path_factory.mktemp("soffice-probe")
    probe = folder / "probe.docx"
    doc = Document()
    doc.add_paragraph("probe")
    doc.save(str(probe))
    return docx_to_pdf(probe, folder, timeout=180) is not None


@pytest.fixture
def require_pdf(pdf_available: bool) -> None:
    if not pdf_available:
        pytest.skip("LibreOffice assente o non funzionante: salto il test PDF")
