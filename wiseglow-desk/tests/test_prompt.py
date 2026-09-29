"""Tests for agent/prompt.py (Italian system prompt)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wiseglow_desk.agent.prompt import MAX_WORDS, build_system_prompt, word_count  # noqa: E402
from wiseglow_desk.agent.tools import TOOL_NAMES  # noqa: E402
from wiseglow_desk.docgen.brand import Brand  # noqa: E402
from wiseglow_desk.docgen.catalog import DOC_TYPES  # noqa: E402

try:
    from wiseglow_desk.config import Settings  # type: ignore
except Exception:  # noqa: BLE001 - config.py belongs to another agent

    @dataclass(frozen=True)
    class Settings:  # type: ignore[no-redef]
        feishu_app_id: str = "cli_x"
        feishu_app_secret: str = "secret"
        feishu_domain: str = "feishu"
        anthropic_api_key: str | None = None
        model: str = "claude-fable-5-1"
        effort: str = "high"
        workspace_dir: Path = Path("workspace")
        brand_path: Path = Path("brand/brand.yaml")
        trigger_mode: str = "mention"
        allow_bash: bool = False
        max_budget_usd_per_turn: float = 5.0
        max_turns: int = 60
        max_parallel_chats: int = 2
        allowed_chat_ids: tuple[str, ...] = ()
        log_level: str = "INFO"


BRAND_PATH = ROOT / "brand" / "brand.yaml"


def build(tmp_path: Path, **kw: object) -> str:
    settings = Settings(brand_path=BRAND_PATH, workspace_dir=tmp_path / "ws", **kw)  # type: ignore[arg-type]
    return build_system_prompt(Brand.from_yaml(BRAND_PATH), settings, tmp_path / "ws")


def test_prompt_is_italian_with_six_sections(tmp_path: Path) -> None:
    prompt = build(tmp_path)
    assert prompt.startswith("# Chi sei")
    for heading in ("# Chi sei", "# Documenti", "# Materiali", "# Gruppo", "# Stile", "# Confini"):
        assert heading in prompt
    assert "Sei Claude, collega del team WiseGlow in questo gruppo Feishu" in prompt


def test_prompt_key_rules(tmp_path: Path) -> None:
    prompt = build(tmp_path)
    # working style
    assert "[DA CONFERMARE: prezzo]" in prompt
    assert "una sola domanda" in prompt.lower()
    assert "italiano, cinese o inglese" in prompt
    # documents
    assert "SOLO con lo strumento `render_document`" in prompt
    assert "stesso `document_id` crea una nuova versione" in prompt
    assert "`get_document`" in prompt and "solo ciò che è stato chiesto" in prompt
    assert "PDF" in prompt and "DOCX" in prompt
    assert "non inventare mai partite IVA" in prompt
    # materials and memory
    assert "`read_office_file`" in prompt and "`save_reference`" in prompt
    assert "knowledge/NOTES.md" in prompt
    # group
    assert "<contesto_gruppo>" in prompt
    # style (Fable guidance in Italian)
    assert "Parti dal risultato" in prompt
    assert "prosa manierata" in prompt
    assert "`send_message`" in prompt
    assert "non dire mai che un file è stato inviato" in prompt.lower()
    # harness facts: between-call text is invisible; never end the turn on a promise
    assert "non arriva in chat" in prompt
    assert "rileggi l'ultimo paragrafo" in prompt and "fallo adesso con gli strumenti" in prompt
    assert "senza narrare" not in prompt
    # boundaries
    assert "Non inviare mai nulla al di fuori di questa chat" in prompt
    assert "brand.yaml" in prompt and "bozze" in prompt


def test_prompt_mentions_every_tool_and_catalog(tmp_path: Path) -> None:
    prompt = build(tmp_path)
    for name in TOOL_NAMES:
        assert f"`{name}`" in prompt, name
    for code in DOC_TYPES:
        assert f"`{code}`" in prompt, code
    assert "Natura non vincolante" in prompt  # a skeleton heading from the catalog


def test_prompt_contains_paths_and_brand_data(tmp_path: Path) -> None:
    prompt = build(tmp_path)
    ws = (tmp_path / "ws").resolve()
    assert str(ws) in prompt
    assert str(ws / "inbox") in prompt
    assert str(ws / "knowledge" / "NOTES.md") in prompt
    assert str(ws / "knowledge" / "templates") in prompt
    brand = Brand.from_yaml(BRAND_PATH)
    assert brand.company.name in prompt
    assert brand.company.vat in prompt
    assert brand.colors.primary in prompt and brand.colors.secondary in prompt
    assert brand.fonts.heading in prompt and brand.fonts.body in prompt
    assert str(BRAND_PATH.parent.resolve()) in prompt
    assert str(brand.logo_path) in prompt


def test_prompt_length_bound(tmp_path: Path) -> None:
    prompt = build(tmp_path)
    assert word_count(prompt) <= MAX_WORDS
    assert word_count(prompt) > 600  # not accidentally truncated


def test_prompt_bash_flag(tmp_path: Path) -> None:
    assert "`Bash`" not in build(tmp_path, allow_bash=False)
    assert "`Bash`" in build(tmp_path, allow_bash=True)


def test_prompt_no_english_guidance_pasted(tmp_path: Path) -> None:
    prompt = build(tmp_path)
    for phrase in ("Lead with the outcome", "mannered prose", "When you have enough information to act"):
        assert phrase not in prompt
