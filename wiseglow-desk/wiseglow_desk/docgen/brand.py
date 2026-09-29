"""Brand: the WiseGlow visual identity as data (``brand/brand.yaml``).

Keys are fixed; values may be edited by the user (or by the agent through
``update_brand`` when explicitly asked).  Missing keys fall back to the
defaults below so an incomplete YAML never breaks rendering.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


class BrandError(ValueError):
    """Raised when brand.yaml holds an invalid value."""


@dataclass
class Company:
    name: str = "WiseGlow S.r.l."
    tagline: str = "Energia solare, semplice."
    address: str = "Piazza Pericle Fazzini 8, 63074 San Benedetto del Tronto (AP)"
    vat: str = "P.IVA e C.F. 02561900446"
    email: str = "contatti@wiseglow.com"
    phone: str = "+39 …"
    website: str = "www.wiseglow.com"


@dataclass
class Colors:
    primary: str = "#243F58"
    secondary: str = "#FF670D"
    text: str = "#222222"
    muted: str = "#6B6B6B"
    table_header_text: str = "#FFFFFF"
    table_alt_row: str = "#F4F6F9"
    callout_bg: str = "#EEF3FA"


@dataclass
class Fonts:
    heading: str = "Sora"
    body: str = "Inter"
    fallback: str = "Arial"
    body_cjk: str = "PingFang SC"


@dataclass
class Logo:
    file: str = "logo.png"
    width_cm: float = 4.6
    max_height_cm: float = 1.6  # header cap: a square/portrait logo shrinks to fit (aspect ratio kept)


@dataclass
class Margins:
    top: float = 2.3
    bottom: float = 2.1
    left: float = 2.3
    right: float = 2.3


@dataclass
class Page:
    size: str = "A4"
    margins_cm: Margins = field(default_factory=Margins)


@dataclass
class Footer:
    text: str = "WiseGlow S.r.l. · Piazza Pericle Fazzini 8, San Benedetto del Tronto (AP) · P.IVA 02561900446 · contatti@wiseglow.com"
    page_numbers: bool = True
    confidential_label: str = "Riservato"


DEFAULT_LABELS: dict[str, dict[str, str]] = {
    "it": {
        "date": "Data", "place": "Luogo", "reference": "Rif.", "recipient": "Spett.le",
        "signatures": "Firme", "attachments": "Allegati", "page": "Pagina", "of": "di", "draft": "Bozza",
    },
    "en": {
        "date": "Date", "place": "Place", "reference": "Ref.", "recipient": "To",
        "signatures": "Signatures", "attachments": "Attachments", "page": "Page", "of": "of", "draft": "Draft",
    },
    "zh": {
        "date": "日期", "place": "地点", "reference": "编号", "recipient": "致",
        "signatures": "签署", "attachments": "附件", "page": "第", "of": "页，共", "draft": "草稿",
    },
}


@dataclass
class Brand:
    company: Company = field(default_factory=Company)
    colors: Colors = field(default_factory=Colors)
    fonts: Fonts = field(default_factory=Fonts)
    logo: Logo = field(default_factory=Logo)
    page: Page = field(default_factory=Page)
    footer: Footer = field(default_factory=Footer)
    labels: dict[str, dict[str, str]] = field(default_factory=lambda: copy.deepcopy(DEFAULT_LABELS))
    base_dir: Path | None = None  # folder of brand.yaml; logo path is relative to it

    # -- I/O -----------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, base_dir: Path | None = None) -> "Brand":
        """Build a Brand from a (possibly partial) dict.

        Unknown keys are ignored (and logged), blank/``null`` values fall back
        to the default, and an invalid value raises :class:`BrandError`.
        """
        ignored: list[str] = []
        known = _filter_known(data or {}, _defaults_dict(), ignored)
        if ignored:
            log.warning("brand: chiavi sconosciute ignorate: %s", ", ".join(ignored))
        merged = _deep_merge(_defaults_dict(), known)
        logo, page = merged["logo"], merged["page"]
        try:
            return cls(
                company=Company(**merged["company"]),
                colors=Colors(**{k: _hex(k, v) for k, v in merged["colors"].items()}),
                fonts=Fonts(**merged["fonts"]),
                logo=Logo(
                    file=str(logo["file"]),
                    width_cm=float(logo["width_cm"]),
                    max_height_cm=float(logo["max_height_cm"]),
                ),
                page=Page(size=str(page["size"]), margins_cm=Margins(**{k: float(v) for k, v in page["margins_cm"].items()})),
                footer=Footer(**merged["footer"]),
                labels=merged["labels"],
                base_dir=base_dir,
            )
        except BrandError:
            raise
        except (TypeError, ValueError) as exc:
            raise BrandError(f"brand.yaml non valido: {exc}") from exc

    @classmethod
    def from_yaml(cls, path: Path | str) -> "Brand":
        path = Path(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:  # syntax errors are not ValueError: translate them
            raise BrandError(f"Il file brand {path} non è YAML valido: {exc}") from exc
        if not isinstance(data, dict):
            raise BrandError(f"Il file brand {path} non contiene una mappa YAML valida.")
        return cls.from_dict(data, base_dir=path.resolve().parent)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("base_dir", None)
        return d

    def to_yaml(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False, width=100)
        header = "# Identità visiva WiseGlow: modifica i valori, non le chiavi.\n"
        path.write_text(header + text, encoding="utf-8")
        return path

    # -- helpers -------------------------------------------------------------

    @property
    def logo_path(self) -> Path:
        """Absolute path of the logo file (may not exist)."""
        p = Path(self.logo.file)
        if not p.is_absolute() and self.base_dir is not None:
            p = self.base_dir / p
        return p.resolve()

    def labels_for(self, language: str) -> dict[str, str]:
        """Fixed renderer labels for ``language`` with Italian as fallback."""
        out = dict(DEFAULT_LABELS["it"])
        out.update(self.labels.get("it", {}))
        out.update(self.labels.get(language, {}))
        return out

    def summary(self) -> str:
        """Short human-readable summary (Italian) for prompts and /status."""
        c, col, f = self.company, self.colors, self.fonts
        return (
            f"{c.name} — {c.tagline} · {c.address} · {c.vat} · {c.email}\n"
            f"Colori: primario {col.primary}, accento {col.secondary}, testo {col.text}, attenuato {col.muted}\n"
            f"Font: titoli {f.heading}, testo {f.body} (CJK {f.body_cjk}, fallback {f.fallback})\n"
            f"Logo: {self.logo_path} ({'presente' if self.logo_path.exists() else 'MANCANTE'}), "
            f"larghezza {self.logo.width_cm} cm"
        )


# ----------------------------------------------------------------------------
# module-level API
# ----------------------------------------------------------------------------


def load_brand(path: Path | str) -> Brand:
    """Load ``brand.yaml``; a missing file yields the defaults (with a warning)."""
    path = Path(path)
    if not path.exists():
        log.warning("brand.yaml non trovato in %s: uso i valori predefiniti", path)
        return Brand(base_dir=path.resolve().parent)
    return Brand.from_yaml(path)


def save_brand(brand: Brand, path: Path | str) -> Path:
    return brand.to_yaml(path)


def update_brand(path: Path | str, patch: dict[str, Any]) -> Brand:
    """Deep-merge ``patch`` (known keys only) into the YAML at ``path`` and save."""
    path = Path(path)
    current = load_brand(path).to_dict()
    known = _defaults_dict()
    ignored: list[str] = []
    merged = _deep_merge(current, _filter_known(patch or {}, known, ignored))
    if ignored:
        log.warning("update_brand: chiavi ignorate perché sconosciute: %s", ", ".join(ignored))
    brand = Brand.from_dict(merged, base_dir=path.resolve().parent)
    brand.to_yaml(path)
    return brand


# ----------------------------------------------------------------------------
# internals
# ----------------------------------------------------------------------------


def _defaults_dict() -> dict[str, Any]:
    return Brand().to_dict()


def _hex(key: str, value: Any) -> str:
    m = _HEX_RE.match(str(value).strip())
    if not m:
        raise BrandError(f"Colore non valido per '{key}': {value!r} (atteso #RRGGBB)")
    return "#" + m.group(1).upper()


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge of ``patch`` over ``base``; a ``None`` value keeps the base value."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _filter_known(patch: dict[str, Any], known: dict[str, Any], ignored: list[str], prefix: str = "") -> dict[str, Any]:
    """Keep only keys that exist in ``known`` (labels: any language, known label keys)."""
    out: dict[str, Any] = {}
    for k, v in patch.items():
        full = f"{prefix}{k}"
        if k not in known:
            ignored.append(full)
            continue
        if k == "labels" and not prefix:
            out[k] = _filter_labels(v, ignored, full) if isinstance(v, dict) else {}
        elif isinstance(v, dict) and isinstance(known[k], dict):
            out[k] = _filter_known(v, known[k], ignored, full + ".")
        else:
            out[k] = v
    return out


def _filter_labels(value: dict[str, Any], ignored: list[str], prefix: str) -> dict[str, Any]:
    allowed = set(DEFAULT_LABELS["it"])
    out: dict[str, Any] = {}
    for lang, labels in value.items():
        if not isinstance(labels, dict):
            ignored.append(f"{prefix}.{lang}")
            continue
        out[lang] = {}
        for k, v in labels.items():
            if k not in allowed:
                ignored.append(f"{prefix}.{lang}.{k}")
            elif v is not None:  # a blank label keeps the default
                out[lang][k] = str(v)
    return out
