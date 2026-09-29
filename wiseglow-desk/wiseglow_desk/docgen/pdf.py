"""DOCX → PDF through LibreOffice (``soffice --headless``).

Each conversion uses a private ``-env:UserInstallation`` profile directory so
concurrent conversions never collide on the shared LibreOffice lock file.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

log = logging.getLogger(__name__)

_MAC_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


def find_soffice() -> str | None:
    """Locate the LibreOffice binary: env WISEGLOW_SOFFICE, PATH, then the macOS app bundle."""
    env = os.environ.get("WISEGLOW_SOFFICE", "").strip()
    if env:
        if Path(env).is_file():
            return env
        log.warning("WISEGLOW_SOFFICE=%s non è un file eseguibile: lo ignoro", env)
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if Path(_MAC_SOFFICE).is_file():
        return _MAC_SOFFICE
    return None


def docx_to_pdf(docx_path: Path | str, out_dir: Path | str, timeout: int = 120) -> Path | None:
    """Convert ``docx_path`` to ``<out_dir>/<stem>.pdf``; return None (and log) on failure."""
    docx_path, out_dir = Path(docx_path), Path(out_dir)
    soffice = find_soffice()
    if soffice is None:
        log.warning("LibreOffice (soffice) non trovato: PDF non generato per %s", docx_path.name)
        return None
    if not docx_path.is_file():
        log.error("File DOCX inesistente: %s", docx_path)
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (docx_path.stem + ".pdf")
    profile = Path(tempfile.mkdtemp(prefix="wiseglow-soffice-"))
    cmd = [
        soffice,
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--norestore",
        "--nologo",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(docx_path),
    ]
    try:
        # own session: on timeout the whole group (wrapper → oosplash → soffice.bin) can be killed
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as exc:
        shutil.rmtree(profile, ignore_errors=True)
        log.error("Impossibile avviare soffice (%s): %s", soffice, exc)
        return None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        log.error("Conversione PDF interrotta dopo %ss: %s", timeout, docx_path.name)
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if proc.returncode != 0 or not pdf_path.is_file():
        log.error(
            "Conversione PDF fallita per %s (exit %s): %s",
            docx_path.name,
            proc.returncode,
            (stderr or stdout or "").strip()[-800:],
        )
        return None
    return pdf_path


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL ``proc`` and every process in its session, then reap it."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX
            proc.kill()
    except ProcessLookupError:
        pass
    proc.communicate()


def pdf_page_count(path: Path | str) -> int:
    """Number of pages of a PDF (via pypdf)."""
    return len(PdfReader(str(path)).pages)
