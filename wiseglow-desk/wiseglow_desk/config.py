"""Runtime settings loaded from the environment (optionally seeded by ``.env``).

``load_settings`` reads ``.env`` with python-dotenv (never overriding
variables that are already set in the process environment), then builds an
immutable :class:`Settings`. Relative paths are made absolute against the
project folder (the one containing ``run.py``) so the bot behaves the same
whether it is started from a terminal or from a LaunchAgent.

Every validation problem raises :class:`ConfigError` with an Italian message
that names the offending variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Mapping

from dotenv import load_dotenv

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE: Path = PROJECT_ROOT / ".env"

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
TRIGGER_MODES: tuple[str, ...] = ("mention", "all")
DOMAINS: tuple[str, ...] = ("feishu", "lark")
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")

_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "si", "sì", "y"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "n", ""})


class ConfigError(ValueError):
    """A missing or invalid configuration value (message in Italian)."""


@dataclass(frozen=True)
class Settings:
    """Everything the bot needs at runtime; built by :func:`load_settings`.

    The Feishu credentials are mandatory at load time (``load_settings`` raises
    :class:`ConfigError`); they default to ``""`` here only so offline commands
    (``run.py --render``) and tests can build a Settings without them.
    """

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = "feishu"  # "feishu" → FEISHU_DOMAIN, "lark" → LARK_DOMAIN
    anthropic_api_key: str | None = None  # None → rely on the process env; warn at startup
    model: str = "claude-fable-5-1"
    effort: str = "high"  # low|medium|high|xhigh|max
    workspace_dir: Path = Path("workspace")  # absolute after load
    brand_path: Path = Path("brand/brand.yaml")  # absolute after load
    trigger_mode: str = "mention"  # "mention": @/DM/reply-to-bot only; "all": every group message
    allow_bash: bool = False
    max_budget_usd_per_turn: float = 5.0
    max_turns: int = 60
    max_parallel_chats: int = 2
    turn_timeout_s: float = 900.0  # wall-clock cap for one Claude turn (a hung tool must not freeze a chat)
    allowed_chat_ids: tuple[str, ...] = ()  # empty = every chat the bot is in
    log_level: str = "INFO"

    @property
    def has_anthropic_key(self) -> bool:
        """True when a key is configured here or already in the environment."""
        return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def chat_allowed(self, chat_id: str) -> bool:
        return not self.allowed_chat_ids or chat_id in self.allowed_chat_ids


# ---------------------------------------------------------------------------
# parsing helpers (each raises ConfigError with the variable name)
# ---------------------------------------------------------------------------


def _missing(name: str) -> ConfigError:
    return ConfigError(
        f"Variabile mancante: {name}. Aggiungila al file .env (vedi .env.example) "
        "oppure esportala nell'ambiente."
    )


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise _missing(name)
    return value


def _optional(env: Mapping[str, str], name: str, default: str) -> str:
    value = (env.get(name) or "").strip()
    return value if value else default


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...], *, lower: bool = True) -> str:
    raw = _optional(env, name, default)
    value = raw.lower() if lower else raw.upper()
    if value not in allowed:
        raise ConfigError(f"Valore non valido per {name}: {raw!r}. Valori ammessi: {', '.join(allowed)}.")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if raw == "":
        return default
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    raise ConfigError(f"Valore non valido per {name}: {raw!r}. Usa true/false.")


def _number(env: Mapping[str, str], name: str, default: float, convert: Callable[[str], float], *, minimum: float) -> float:
    raw = (env.get(name) or "").strip()
    if raw == "":
        return default
    try:
        value = convert(raw)
    except ValueError as exc:
        raise ConfigError(f"Valore non valido per {name}: {raw!r} (atteso un numero).") from exc
    if value < minimum:
        raise ConfigError(f"Valore non valido per {name}: {raw!r} (deve essere almeno {minimum:g}).")
    return value


def _csv(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = env.get(name) or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _abs_path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = (env.get(name) or "").strip()
    path = Path(os.path.expanduser(raw)) if raw else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def settings_from_env(env: Mapping[str, str], *, require_feishu: bool = True) -> Settings:
    """Build :class:`Settings` from a mapping (the process env or a test dict)."""
    app_id = _required(env, "FEISHU_APP_ID") if require_feishu else _optional(env, "FEISHU_APP_ID", "")
    app_secret = _required(env, "FEISHU_APP_SECRET") if require_feishu else _optional(env, "FEISHU_APP_SECRET", "")
    api_key = (env.get("ANTHROPIC_API_KEY") or "").strip() or None
    return Settings(
        feishu_app_id=app_id,
        feishu_app_secret=app_secret,
        feishu_domain=_choice(env, "FEISHU_DOMAIN", "feishu", DOMAINS),
        anthropic_api_key=api_key,
        model=_optional(env, "WISEGLOW_MODEL", "claude-fable-5-1"),
        effort=_choice(env, "WISEGLOW_EFFORT", "high", EFFORT_LEVELS),
        workspace_dir=_abs_path(env, "WISEGLOW_WORKSPACE", Path("workspace")),
        brand_path=_abs_path(env, "WISEGLOW_BRAND", Path("brand/brand.yaml")),
        trigger_mode=_choice(env, "WISEGLOW_TRIGGER_MODE", "mention", TRIGGER_MODES),
        allow_bash=_bool(env, "WISEGLOW_ALLOW_BASH", False),
        max_budget_usd_per_turn=float(_number(env, "WISEGLOW_MAX_BUDGET_USD", 5.0, float, minimum=0.01)),
        max_turns=int(_number(env, "WISEGLOW_MAX_TURNS", 60, int, minimum=1)),
        max_parallel_chats=int(_number(env, "WISEGLOW_MAX_PARALLEL_CHATS", 2, int, minimum=1)),
        turn_timeout_s=float(_number(env, "WISEGLOW_TURN_TIMEOUT", 900, int, minimum=30)),
        allowed_chat_ids=_csv(env, "WISEGLOW_ALLOWED_CHATS"),
        log_level=_choice(env, "WISEGLOW_LOG_LEVEL", "INFO", LOG_LEVELS, lower=False),
    )


def load_settings(env_file: Path | None = None, *, require_feishu: bool = True) -> Settings:
    """Read ``.env`` (without overriding the environment) and build :class:`Settings`.

    ``env_file`` defaults to ``<project>/.env``; a missing file is fine.
    ``require_feishu=False`` is for offline commands (``run.py --render``).
    """
    path = Path(env_file) if env_file is not None else DEFAULT_ENV_FILE
    if path.is_file():
        load_dotenv(path, override=False)
    elif env_file is not None:
        raise ConfigError(f"File di configurazione non trovato: {path}")
    return settings_from_env(os.environ, require_feishu=require_feishu)


def describe(settings: Settings) -> str:
    """One line per field, secrets masked (for logs and ``--check``)."""
    secret_fields = {"feishu_app_secret", "anthropic_api_key"}
    lines = []
    for f in fields(settings):
        value = getattr(settings, f.name)
        if f.name in secret_fields:
            value = "impostata" if value else "assente"
        lines.append(f"{f.name}: {value}")
    return "\n".join(lines)
