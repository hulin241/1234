"""Catalog of the document types WiseGlow Desk knows how to draft.

Each entry carries the trilingual name, a one-line Italian description and a
*skeleton*: the typical section headings (Italian) the model can start from.
The catalog is informative only — any ``doc_type`` code may carry any
sections; ``GENERIC`` has no skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DocType:
    """One document type of the catalog."""

    code: str
    name_it: str
    name_zh: str
    name_en: str
    description_it: str
    skeleton: list[str] = field(default_factory=list)


def _dt(
    code: str,
    name_it: str,
    name_zh: str,
    name_en: str,
    description_it: str,
    skeleton: str = "",
) -> DocType:
    """Build a DocType from a ``;``-separated skeleton string."""
    headings = [h.strip() for h in skeleton.split(";") if h.strip()]
    return DocType(code, name_it, name_zh, name_en, description_it, headings)


_TYPES: list[DocType] = [
    _dt(
        "NBO",
        "Offerta non vincolante",
        "非约束性报价",
        "Non-binding offer",
        "Offerta preliminare, non vincolante, per l'acquisto di un terreno, di un impianto o di una società di progetto.",
        "Premessa; Oggetto; Corrispettivo e condizioni economiche; Condizioni sospensive e due diligence; "
        "Tempistiche; Esclusiva e riservatezza; Natura non vincolante; Legge applicabile",
    ),
    _dt(
        "LOI",
        "Lettera di intenti",
        "意向书",
        "Letter of intent",
        "Lettera di intenti che fissa i termini principali di un'operazione prima del contratto definitivo.",
        "Premesse; Oggetto dell'operazione; Termini economici principali; Due diligence; Esclusiva; "
        "Riservatezza; Natura non vincolante; Durata; Legge applicabile e foro",
    ),
    _dt(
        "INTERESSE_TERRENO",
        "Manifestazione di interesse per terreno",
        "土地意向书",
        "Expression of interest for land",
        "Lettera a un proprietario terriero per proporre acquisto, diritto di superficie o affitto per un impianto fotovoltaico.",
        "Chi siamo; Il terreno; La nostra proposta (acquisto / diritto di superficie / affitto); Prossimi passi; Contatti",
    ),
    _dt(
        "MANDATO_VENDITA",
        "Mandato di vendita",
        "销售委托",
        "Sales mandate",
        "Mandato con cui un soggetto incarica WiseGlow (o un terzo) di vendere un bene o un progetto.",
        "Parti; Oggetto del mandato; Durata; Compenso/provvigione; Obblighi delle parti; Esclusiva; Recesso; Foro",
    ),
    _dt(
        "PROPOSTA_BANCA",
        "Proposta per banche e istituti",
        "银行及机构提案",
        "Proposal for banks and institutions",
        "Presentazione di un progetto a banche o istituti finanziari con richiesta di finanziamento.",
        "Sintesi; La società; Il progetto; Piano economico-finanziario; Richiesta; Garanzie; Allegati",
    ),
    _dt(
        "PPA_LIGHT",
        "Contratto PPA Light",
        "PPA Light 合同",
        "PPA Light contract",
        "Contratto di fornitura di energia da impianto fotovoltaico in modalità PPA semplificata.",
        "Parti; Definizioni; Oggetto; Impianto; Prezzo dell'energia; Durata; Obblighi; Manutenzione; Risoluzione; Legge e foro",
    ),
    _dt(
        "PPA_LIGHT_PRO_OFFERTA",
        "Offerta PPA Light Pro",
        "PPA Light Pro 报价",
        "PPA Light Pro offer",
        "Offerta commerciale per la formula PPA Light Pro (impianto + energia).",
        "Sintesi dell'offerta; Impianto proposto; Condizioni economiche; Servizi inclusi; Tempistiche; Validità",
    ),
    _dt(
        "DDS",
        "Contratto DDS",
        "DDS 合同",
        "DDS contract",
        "Contratto di diritto di superficie (o analogo) per la realizzazione di un impianto.",
        "Parti; Oggetto; Durata; Corrispettivo; Obblighi; Riservatezza; Risoluzione; Foro",
    ),
    _dt(
        "EPC",
        "Contratto EPC",
        "EPC 合同",
        "EPC contract",
        "Contratto chiavi in mano per progettazione, fornitura e installazione di un impianto.",
        "Parti; Definizioni; Oggetto; Progettazione; Fornitura; Installazione; Collaudo; Prezzo e pagamenti; "
        "Garanzie; Penali; Risoluzione; Foro",
    ),
    _dt(
        "EPC_COLLABORAZIONE",
        "Contratto di collaborazione EPC",
        "EPC 合作合同",
        "EPC collaboration contract",
        "Accordo di collaborazione tra WiseGlow e un installatore/EPC partner.",
        "Parti; Oggetto della collaborazione; Ruoli; Condizioni economiche; Durata; Esclusiva; Riservatezza; Foro",
    ),
    _dt(
        "PROCACCIATORE",
        "Contratto procacciatore d'affari",
        "Procacciatore 合同",
        "Business finder agreement",
        "Contratto con un procacciatore d'affari che segnala clienti o terreni contro provvigione.",
        "Parti; Oggetto; Provvigioni; Durata; Obblighi; Assenza di subordinazione; Riservatezza; Foro",
    ),
    _dt(
        "COLLABORAZIONE",
        "Contratto di collaborazione",
        "合作合同",
        "Collaboration contract",
        "Accordo generico di collaborazione commerciale o tecnica.",
        "Parti; Oggetto; Attività; Corrispettivi; Durata; Riservatezza; Foro",
    ),
    _dt(
        "OM",
        "Contratto O&M",
        "O&M 合同",
        "O&M contract",
        "Contratto di gestione e manutenzione (Operation & Maintenance) di un impianto.",
        "Parti; Oggetto; Servizi inclusi; Livelli di servizio; Corrispettivo; Durata; Responsabilità; Risoluzione; Foro",
    ),
    _dt(
        "OFFERTA_PRODOTTO",
        "Offerta prodotto",
        "产品报价",
        "Product offer",
        "Offerta commerciale per la fornitura di moduli, inverter, sistemi di accumulo o altri prodotti.",
        "Sintesi; Prodotti e quantità; Prezzi; Condizioni di fornitura; Validità",
    ),
    _dt(
        "GENERIC",
        "Documento generico",
        "通用文件",
        "Generic document",
        "Qualsiasi altro documento (verbale, memo, lettera, relazione): struttura libera.",
    ),
]

DOC_TYPES: dict[str, DocType] = {t.code: t for t in _TYPES}


def catalog_markdown() -> str:
    """Return a compact Italian list of the catalog for the system prompt."""
    lines: list[str] = []
    for t in DOC_TYPES.values():
        line = f"- `{t.code}` — {t.name_it} ({t.name_zh}): {t.description_it}"
        if t.skeleton:
            line += " Sezioni tipiche: " + "; ".join(t.skeleton) + "."
        lines.append(line)
    return "\n".join(lines)
