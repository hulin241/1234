#!/usr/bin/env python3
"""WiseGlow Desk entrypoint.

    python run.py                      start the bot (Feishu long connection)
    python run.py --check              verify config, brand, LibreOffice, Feishu, API key
    python run.py --render FILE.json   render a Document JSON with the brand, no Feishu
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wiseglow_desk import __version__  # noqa: E402
from wiseglow_desk.bot import Desk, checks_ok, format_report, render_sample, run_checks  # noqa: E402
from wiseglow_desk.config import ConfigError, Settings, load_settings  # noqa: E402
from wiseglow_desk.docgen.brand import BrandError  # noqa: E402
from wiseglow_desk.log import setup_logging  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="WiseGlow Desk: il bot Feishu che prepara documenti con l'identità visiva WiseGlow.",
        epilog="Senza opzioni avvia il bot e resta in ascolto (Ctrl+C per fermarlo).",
    )
    parser.add_argument("--check", action="store_true", help="verifica configurazione, brand, LibreOffice, Feishu e chiave API")
    parser.add_argument("--render", metavar="FILE.json", help="genera DOCX/PDF da un documento JSON senza Feishu (test del brand)")
    parser.add_argument("--out", metavar="CARTELLA", help="cartella di uscita per --render (default: workspace/render)")
    parser.add_argument("--no-pdf", action="store_true", help="con --render: salta la conversione in PDF")
    parser.add_argument("--env-file", metavar="FILE", type=Path, help="file .env alternativo (default: .env accanto a run.py)")
    parser.add_argument("--version", action="version", version=f"WiseGlow Desk {__version__}")
    return parser


def cmd_render(settings: Settings, args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else Path(settings.workspace_dir) / "render"
    try:
        result = render_sample(Path(args.render), settings.brand_path, out_dir, with_pdf=not args.no_pdf)
    except (OSError, ValueError) as exc:
        print(f"Rendering fallito: {exc}", file=sys.stderr)
        return 1
    print(f"DOCX: {result.docx_path}")
    if result.pdf_path:
        print(f"PDF:  {result.pdf_path} ({result.pages} pagine)")
    elif not args.no_pdf:
        print("PDF:  non generato (LibreOffice assente o conversione fallita; vedi il log)")
    return 0


def cmd_check(settings: Settings) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    checks = asyncio.run(run_checks(settings))
    print(format_report(checks))
    return 0 if checks_ok(checks) else 1


def cmd_serve(settings: Settings) -> int:
    log_path = setup_logging(settings.workspace_dir, settings.log_level)
    logging.getLogger("wiseglow").info("Log su file: %s", log_path)
    try:
        desk = Desk(settings)
    except BrandError as exc:  # one Italian line in the LaunchAgent log instead of a traceback
        print(f"Brand non valido: {exc}", file=sys.stderr)
        return 1
    try:
        asyncio.run(desk.run())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file, require_feishu=args.render is None)
    except ConfigError as exc:
        print(f"Configurazione non valida: {exc}", file=sys.stderr)
        return 1
    if args.render:
        return cmd_render(settings, args)
    if args.check:
        return cmd_check(settings)
    return cmd_serve(settings)


if __name__ == "__main__":
    sys.exit(main())
