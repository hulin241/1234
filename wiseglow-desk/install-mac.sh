#!/usr/bin/env bash
#
# install-mac.sh
# Installa WiseGlow Desk come servizio sempre attivo su un Mac (LaunchAgent).
#
# Fa queste cose:
#   1. Controlli: macOS, Python ≥ 3.11 (se manca prova con Homebrew), LibreOffice, font Sora e Inter
#   2. Crea la cartella .venv nel progetto e installa le dipendenze (requirements.txt)
#   3. Verifica che esista .env (se manca lo copia da .env.example e si ferma con le istruzioni);
#      il file contiene segreti, quindi lo rende leggibile solo dal tuo utente (chmod 600)
#   4. Esegue  python run.py --check  (Feishu, chiave Anthropic, brand, LibreOffice)
#   5. Installa il LaunchAgent com.wiseglow.desk: parte al login, si riavvia da solo entro 30 s se cade
#
# Uso (dal Terminale del Mac, dentro la cartella wiseglow-desk o con --dir):
#   bash install-mac.sh                 # installa e avvia
#   bash install-mac.sh --status        # stato del servizio e ultime righe di log
#   bash install-mac.sh --stop          # ferma il servizio (resta installato: --start per riavviarlo)
#   bash install-mac.sh --start         # riavvia un servizio fermato con --stop
#   bash install-mac.sh --uninstall     # rimuove il LaunchAgent (i file del progetto restano)
#   bash install-mac.sh --dry-run       # mostra cosa farebbe senza cambiare nulla
#
# Per provare il bot a mano (.venv/bin/python run.py) ferma prima il servizio con --stop: Feishu
# consegna ogni messaggio a UNA sola istanza collegata, quindi due bot attivi se li spartiscono a caso.
#
# Opzioni:
#   --dir <cartella>      cartella del progetto (default: quella in cui si trova questo script)
#   --python <percorso>   interprete Python da usare per il .venv (default: cerca python3.11+ nel PATH)
#   --skip-check          non eseguire  run.py --check  prima dell'installazione
#
set -euo pipefail

LABEL="com.wiseglow.desk"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/wiseglow-desk.log"
MIN_PY="3.11"
BREW_PY_FORMULA="python@3.11"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
PY_BIN=""
ACTION="install"
DRY_RUN=0
SKIP_CHECK=0
IS_MAC=0
VENV=""
PATH_ENV=""
PLIST_CONTENT=""

if [[ "$(uname -s)" == "Darwin" ]]; then IS_MAC=1; fi

usage() {
  cat <<'USAGE'
Uso: bash install-mac.sh [--dir cartella] [--python percorso] [--skip-check]
                         [--dry-run] [--status] [--stop] [--start] [--uninstall]

Da eseguire nel Terminale del Mac (non via SSH). Senza opzioni installa e avvia
il servizio WiseGlow Desk come LaunchAgent dell'utente corrente.
--stop ferma il servizio senza disinstallarlo (per provare il bot a mano),
--start lo riavvia.
USAGE
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

need_arg() {
  # need_arg <flag> <valore>
  [[ -n "${2:-}" ]] || die "L'opzione $1 richiede un valore"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)        need_arg "$1" "${2:-}"; PROJECT_DIR="$2"; shift 2 ;;
    --python)     need_arg "$1" "${2:-}"; PY_BIN="$2"; shift 2 ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --status)     ACTION="status"; shift ;;
    --stop)       ACTION="stop"; shift ;;
    --start)      ACTION="start"; shift ;;
    --uninstall)  ACTION="uninstall"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage >&2; die "Opzione sconosciuta: $1" ;;
  esac
done

# version_ge A B  →  0 se A >= B (confronta solo le prime tre parti numeriche; compatibile con bash 3.2)
version_ge() {
  local IFS=.
  local -a a b
  read -r -a a <<<"$1"
  read -r -a b <<<"$2"
  local i x y
  for i in 0 1 2; do
    x="${a[$i]:-0}"; y="${b[$i]:-0}"
    x="${x%%[^0-9]*}"; y="${y%%[^0-9]*}"
    x="${x:-0}"; y="${y:-0}"
    (( 10#$x > 10#$y )) && return 0
    (( 10#$x < 10#$y )) && return 1
  done
  return 0
}

# sed invece di ${var//}: da bash 5.2 la & nella stringa di sostituzione ha un significato speciale
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

strip_ansi() {
  local esc
  esc="$(printf '\033')"
  sed -E "s/${esc}\[[0-9;?]*[A-Za-z]//g"
}

# 0 se il file è accessibile solo dal proprietario (modo ottale che termina con 00, es. 600)
private_mode() {
  local mode
  mode="$(stat -c '%a' "$1" 2>/dev/null)" || mode="$(stat -f '%Lp' "$1" 2>/dev/null)" || mode=""
  [[ -n "$mode" && "$mode" == *00 ]]
}

# Versione "X.Y.Z" di un interprete, vuoto se non parte (su macOS senza Xcode CLT python3 è solo uno stub)
python_version() {
  "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true
}

find_python() {
  local candidates c ver
  if [[ -n "$PY_BIN" ]]; then
    ver="$(python_version "$PY_BIN")"
    [[ -n "$ver" ]] || die "L'interprete indicato non funziona: $PY_BIN"
    version_ge "$ver" "$MIN_PY" || die "Python $ver in $PY_BIN è troppo vecchio: serve ≥ $MIN_PY"
    return 0
  fi
  candidates="python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3"
  for c in $candidates; do
    if [[ "$c" == /* ]]; then
      [[ -x "$c" ]] || continue
    else
      c="$(command -v "$c" 2>/dev/null || true)"
      [[ -n "$c" ]] || continue
    fi
    ver="$(python_version "$c")"
    [[ -n "$ver" ]] || continue
    if version_ge "$ver" "$MIN_PY"; then
      PY_BIN="$c"
      return 0
    fi
  done
  return 1
}

install_python_with_brew() {
  command -v brew >/dev/null 2>&1 || die "Non trovo Python ≥ $MIN_PY e nemmeno Homebrew. Installa Homebrew (https://brew.sh) e poi: brew install $BREW_PY_FORMULA"
  if (( DRY_RUN )); then
    warn "Python ≥ $MIN_PY non trovato: in un'installazione reale eseguirei  brew install $BREW_PY_FORMULA"
    return 0
  fi
  log "Python ≥ $MIN_PY non trovato: lo installo con Homebrew ($BREW_PY_FORMULA)"
  brew install "$BREW_PY_FORMULA" || die "brew install $BREW_PY_FORMULA non è riuscito"
  find_python || die "Python installato ma non lo trovo nel PATH. Riapri il Terminale e riprova, oppure usa --python <percorso>"
}

preflight() {
  log "Controlli preliminari"

  if (( ! IS_MAC )); then
    if (( DRY_RUN )); then
      warn "Non siamo su macOS: eseguo solo una simulazione (dry-run)"
    else
      die "Questo script funziona solo su macOS (usa launchd)"
    fi
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    if (( DRY_RUN )); then
      warn "Sei root: l'installazione reale verrebbe rifiutata (il LaunchAgent va installato nel tuo utente)"
    else
      die "Non usare sudo: il LaunchAgent va installato nel tuo utente"
    fi
  fi
  if (( IS_MAC )) && (( ! DRY_RUN )); then
    if [[ "$(launchctl managername 2>/dev/null || true)" != "Aqua" ]]; then
      warn "Sembra una sessione SSH (non la sessione grafica): launchctl potrebbe rifiutare l'installazione. Meglio dal Terminale del Mac."
    fi
  fi

  [[ -d "$PROJECT_DIR" ]] || die "Cartella non trovata: $PROJECT_DIR"
  PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
  [[ -f "$PROJECT_DIR/run.py" && -f "$PROJECT_DIR/requirements.txt" ]] || die "In $PROJECT_DIR non trovo run.py / requirements.txt: indica la cartella wiseglow-desk con --dir"
  VENV="$PROJECT_DIR/.venv"
  ok "Cartella del progetto: $PROJECT_DIR"
  case "$PROJECT_DIR" in
    "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*|"$HOME/Library/Mobile Documents"*|/Volumes/*)
      warn "La cartella è in Scrivania/Documenti/Download/iCloud/disco esterno: macOS può bloccare il servizio in background con una richiesta di autorizzazione."
      warn "Consiglio: sposta il progetto in una cartella come ~/wiseglow-desk" ;;
  esac

  if (( IS_MAC )) && ! xcode-select -p >/dev/null 2>&1; then
    warn "Strumenti da riga di comando di Xcode non trovati: se python3/git non funzionano esegui  xcode-select --install"
  fi

  if ! find_python; then
    if (( IS_MAC )); then
      install_python_with_brew
    else
      die "Python ≥ $MIN_PY non trovato"
    fi
  fi
  ok "Python: $PY_BIN ($(python_version "$PY_BIN"))"
}

check_libreoffice() {
  if [[ -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ]] || command -v soffice >/dev/null 2>&1; then
    ok "LibreOffice trovato (conversione in PDF attiva)"
  else
    warn "LibreOffice non trovato: i documenti usciranno solo in DOCX. Per i PDF:  brew install --cask libreoffice"
  fi
}

check_fonts() {
  local missing="" f
  for f in Sora Inter; do
    if ! ls "$HOME/Library/Fonts" /Library/Fonts 2>/dev/null | grep -qi "^$f"; then
      missing="$missing $f"
    fi
  done
  if [[ -z "$missing" ]]; then
    ok "Font Sora e Inter installati"
  else
    warn "Font non trovati:$missing (verrà usato il font di ripiego). Per installarli:  brew install --cask font-sora font-inter"
  fi
}

setup_venv() {
  log "Ambiente Python (.venv) e dipendenze"
  if (( DRY_RUN )); then
    printf '   [dry-run] %s -m venv %s\n' "$PY_BIN" "$VENV"
    printf '   [dry-run] %s/bin/python -m pip install -r %s/requirements.txt\n' "$VENV" "$PROJECT_DIR"
    return 0
  fi
  if [[ ! -x "$VENV/bin/python" ]]; then
    "$PY_BIN" -m venv "$VENV" || die "Creazione del .venv fallita"
  fi
  "$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || warn "Aggiornamento di pip non riuscito, continuo"
  "$VENV/bin/python" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt" || die "Installazione delle dipendenze fallita (vedi errori sopra)"
  ok "Dipendenze installate in $VENV"
}

check_env_file() {
  log "File .env"
  if [[ -f "$PROJECT_DIR/.env" ]]; then
    # contiene l'App Secret di Feishu e la chiave Anthropic: non deve essere leggibile dagli altri utenti del Mac
    if private_mode "$PROJECT_DIR/.env"; then
      ok ".env presente"
    elif (( DRY_RUN )); then
      warn ".env leggibile da altri utenti del Mac: in un'installazione reale eseguirei  chmod 600 $PROJECT_DIR/.env"
    else
      chmod 600 "$PROJECT_DIR/.env"
      ok ".env presente (ora leggibile solo dal tuo utente: chmod 600)"
    fi
    return 0
  fi
  if (( DRY_RUN )); then
    warn ".env mancante: in un'installazione reale lo copierei da .env.example (chmod 600) e mi fermerei"
    return 0
  fi
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  chmod 600 "$PROJECT_DIR/.env"
  die "Ho creato $PROJECT_DIR/.env da .env.example. Aprilo, inserisci FEISHU_APP_ID, FEISHU_APP_SECRET e ANTHROPIC_API_KEY, poi rilancia:  bash $0"
}

run_check() {
  if (( SKIP_CHECK )); then
    warn "Salto  run.py --check  come richiesto"
    return 0
  fi
  log "Verifica della configurazione (run.py --check)"
  if (( DRY_RUN )); then
    printf '   [dry-run] cd %s && %s/bin/python run.py --check\n' "$PROJECT_DIR" "$VENV"
    return 0
  fi
  if ! (cd "$PROJECT_DIR" && "$VENV/bin/python" run.py --check); then
    die "Il controllo ha trovato problemi: correggi il .env (o il brand) e rilancia lo script. Per installare comunque: --skip-check"
  fi
}

build_plist() {
  PATH_ENV="$VENV/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  PLIST_CONTENT="$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$VENV/bin/python")</string>
    <string>$(xml_escape "$PROJECT_DIR/run.py")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$PROJECT_DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(xml_escape "$PATH_ENV")</string>
    <key>HOME</key>
    <string>$(xml_escape "$HOME")</string>
    <key>LANG</key>
    <string>en_US.UTF-8</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$LOG")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$LOG")</string>
</dict>
</plist>
EOF
)"
}

install_agent() {
  local uid
  uid="$(id -u)"
  log "Installazione del LaunchAgent: $PLIST"
  if (( DRY_RUN )); then
    printf '%s\n' "$PLIST_CONTENT" | sed 's/^/   | /'
    printf '   [dry-run] launchctl bootstrap gui/%s %s\n' "$uid" "$PLIST"
    return 0
  fi
  mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
  if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
    log "Il servizio è già in esecuzione: lo fermo prima di reinstallarlo"
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
    local i
    for i in $(seq 1 30); do
      launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1 || break
      sleep 1
    done
    if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
      die "Il vecchio servizio non si è fermato entro 30 s. Esegui  launchctl bootout gui/$uid/$LABEL  e riprova"
    fi
  fi
  # archivio del log precedente: così le righe che vedremo sono di questo avvio
  if [[ -s "$LOG" ]]; then
    mv -f "$LOG" "$LOG.1"
  fi
  printf '%s\n' "$PLIST_CONTENT" > "$PLIST"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST" >/dev/null || die "Il plist generato non è valido (bug dello script): inviami il contenuto di $PLIST"
  fi
  launchctl bootstrap "gui/$uid" "$PLIST" || die "launchctl bootstrap fallito (vedi errore sopra)"
  ok "Servizio avviato (parte al login, si riavvia entro 30 s se si ferma)"
}

show_result() {
  log "Attendo l'avvio del bot (max 40 s)"
  local i started=""
  for i in $(seq 1 20); do
    sleep 2
    if strip_ansi < "$LOG" 2>/dev/null | grep -q 'WiseGlow Desk avviato'; then
      started=1
      break
    fi
  done
  if [[ -n "$started" ]]; then
    ok "Il bot è in ascolto su Feishu"
  else
    warn "Non vedo ancora la riga di avvio nel log. Tra poco esegui  bash $0 --status ; il log è in $LOG"
    warn "Se il servizio si riavvia in continuazione, fermalo e prova a mano:  bash $0 --stop && cd \"$PROJECT_DIR\" && .venv/bin/python run.py"
    warn "(con il servizio attivo Feishu spartirebbe i messaggi tra le due istanze); alla fine:  bash $0 --start"
  fi
  cat <<NEXT

Prossimi passi:
  1. In Feishu aggiungi il bot al gruppo (impostazioni del gruppo → 群机器人 → 添加机器人)
  2. Scrivigli in privato o menzionalo nel gruppo: «@WiseGlow Desk /help»
  3. Sostituisci brand/logo.png con il logo reale e controlla i dati in brand/brand.yaml

Stato:      bash $0 --status
Log:        tail -f "$LOG"
Pausa:      bash $0 --stop      (poi  bash $0 --start ; da fare prima di avviare run.py a mano)
Rimozione:  bash $0 --uninstall
NEXT
}

check_sleep() {
  command -v pmset >/dev/null 2>&1 || return 0
  local s
  s="$(pmset -g 2>/dev/null | awk '$1=="sleep"{print $2; exit}')"
  if [[ -n "$s" && "$s" != "0" ]]; then
    warn "Il Mac può andare in stop automatico (pmset sleep=$s). In stop il bot non risponde."
    warn "Consiglio: Impostazioni di Sistema → Batteria/Risparmio energia → disattiva lo stop automatico"
  fi
}

status() {
  local uid
  uid="$(id -u)"
  if launchctl print "gui/$uid/$LABEL" 2>/dev/null | grep -E '^[[:space:]]*(state|pid|last exit code) ='; then
    :
  else
    echo "LaunchAgent non caricato (non installato o rimosso)"
  fi
  if [[ -f "$LOG" ]]; then
    echo "--- Ultime righe di log: $LOG ($(du -h "$LOG" | awk '{print $1}')) ---"
    strip_ansi < "$LOG" | grep -v '^[[:space:]]*$' | tail -n 40
    if strip_ansi < "$LOG" | grep -qiE 'Configurazione non valida|ANTHROPIC_API_KEY assente|Brand non valido'; then
      echo
      echo "  ! Il log segnala un problema di configurazione: controlla il file .env (o brand/brand.yaml) e poi rilancia  bash $0"
    fi
  else
    echo "(nessun log per ora)"
  fi
}

uninstall() {
  local uid
  uid="$(id -u)"
  launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  ok "LaunchAgent rimosso (il progetto, il .venv e la cartella workspace/ restano)"
}

# Ferma il servizio senza toccare il plist: serve prima di avviare run.py a mano, perché Feishu
# consegna ogni messaggio a una sola istanza collegata e due bot attivi se li spartirebbero a caso.
stop_agent() {
  local uid
  uid="$(id -u)"
  if ! launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
    ok "Il servizio non è in esecuzione"
    return 0
  fi
  launchctl bootout "gui/$uid/$LABEL" || die "launchctl bootout fallito (vedi errore sopra)"
  ok "Servizio fermato. Ora puoi avviare il bot a mano:  cd \"$PROJECT_DIR\" && .venv/bin/python run.py"
  echo "   Per riavviare il servizio:  bash $0 --start"
}

start_agent() {
  local uid
  uid="$(id -u)"
  [[ -f "$PLIST" ]] || die "LaunchAgent non installato ($PLIST mancante): esegui  bash $0"
  if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
    ok "Il servizio è già in esecuzione"
    return 0
  fi
  launchctl bootstrap "gui/$uid" "$PLIST" || die "launchctl bootstrap fallito (vedi errore sopra)"
  ok "Servizio riavviato (log in $LOG)"
}

main() {
  case "$ACTION" in
    status)    status ;;
    stop)      stop_agent ;;
    start)     start_agent ;;
    uninstall) uninstall ;;
    install)
      preflight
      check_libreoffice
      check_fonts
      setup_venv
      check_env_file
      run_check
      build_plist
      install_agent
      if (( ! DRY_RUN )); then show_result; fi
      check_sleep
      ;;
  esac
}

main
