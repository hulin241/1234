#!/usr/bin/env bash
#
# setup-remote-control.sh
# 让这台常开的 Mac 可以从 claude.ai/code（网页）和 Claude 手机 App 远程操控。
#
# 做三件事：
#   1. 体检：claude CLI、版本、登录、Remote Control 可用性、目录信任、首次确认是否已完成
#   2. 在 settings.json 打开 remoteControlAtStartup —— 本机手动开的 claude 会话也自动出现在网页端
#   3. 安装一个 LaunchAgent，常驻运行 `claude remote-control`（登录后自启、退出后 30 秒内自动拉起）
#
# 第一次用之前，必须在项目目录里手动运行一次  claude remote-control  完成 y/n 确认（脚本会检测并提示）。
#
# 用法（在一个你用 claude 打开过的项目目录里执行，要在 Mac 的图形界面终端里跑，不要走 SSH）：
#   bash setup-remote-control.sh                 # 安装并启动
#   bash setup-remote-control.sh --status        # 看运行状态和最近日志
#   bash setup-remote-control.sh --uninstall     # 卸载 LaunchAgent
#   bash setup-remote-control.sh --dry-run       # 只打印将要做的事，不改任何东西
#
# 可选参数：
#   --dir <路径>              用哪个目录当会话的工作目录（默认当前目录）
#   --name <名字>             网页端看到的会话名（默认主机名）
#   --permission-mode <模式>  会话的权限模式（default acceptEdits plan auto dontAsk bypassPermissions manual），默认不传
#   --skip-trust-check        跳过"目录信任 / 首次确认"的检测
#
set -euo pipefail

LABEL="com.claude.remote-control"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/claude-remote-control.log"
CONF_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CONF_DIR/settings.json"
if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
  GLOBAL_CONFIG="$CLAUDE_CONFIG_DIR/.claude.json"
else
  GLOBAL_CONFIG="$HOME/.claude.json"
fi
MIN_VERSION="2.1.200"

PROJECT_DIR="$PWD"
SESSION_NAME=""
PERMISSION_MODE=""
ACTION="install"
DRY_RUN=0
SKIP_TRUST_CHECK=0
IS_MAC=0
CLAUDE_BIN=""
PATH_ENV=""
PLIST_CONTENT=""

if [[ "$(uname -s)" == "Darwin" ]]; then IS_MAC=1; fi

usage() {
  cat <<'USAGE'
用法: bash setup-remote-control.sh [--dir 路径] [--name 名字] [--permission-mode 模式]
                                   [--skip-trust-check] [--dry-run] [--status] [--uninstall]

在一个你用 claude 打开过的项目目录里、在 Mac 的图形界面终端里运行。
默认动作是安装并启动常驻的 Remote Control 服务。
USAGE
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

need_arg() {
  # need_arg <flag> <value>
  [[ -n "${2:-}" ]] || die "参数 $1 后面需要一个值"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)              need_arg "$1" "${2:-}"; PROJECT_DIR="$2"; shift 2 ;;
    --name)             need_arg "$1" "${2:-}"; SESSION_NAME="$2"; shift 2 ;;
    --permission-mode)  need_arg "$1" "${2:-}"; PERMISSION_MODE="$2"; shift 2 ;;
    --skip-trust-check) SKIP_TRUST_CHECK=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    --status)           ACTION="status"; shift ;;
    --uninstall)        ACTION="uninstall"; shift ;;
    -h|--help)          usage; exit 0 ;;
    *)                  usage >&2; die "未知参数: $1" ;;
  esac
done

case "$PERMISSION_MODE" in
  ''|default|acceptEdits|plan|auto|dontAsk|bypassPermissions|manual) ;;
  *) die "无效的 --permission-mode: $PERMISSION_MODE（可选: default acceptEdits plan auto dontAsk bypassPermissions manual）" ;;
esac

# macOS 没装 Xcode 命令行工具时 /usr/bin/python3 只是一个会弹安装框的桩，所以要真的跑一下
have_python3() {
  python3 -c 'import json, os, sys' >/dev/null 2>&1
}

# version_ge A B  →  A >= B 时返回 0（只比较前三段数字，兼容 macOS 自带的 bash 3.2）
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

# 用 sed 而不是 ${var//} 做替换：bash 5.2 起替换串里的 & 有特殊含义，会把 &lt; 变成 <lt;
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

strip_ansi() {
  local esc
  esc="$(printf '\033')"
  sed -E "s/${esc}\[[0-9;?]*[A-Za-z]//g"
}

# 读 ~/.claude.json：目录（或它所在仓库根目录 / 任一上级目录）是否已信任、首次 y/n 确认是否已做过。
# 输出一行 "trusted=yes|no consent=yes|no"；读不到时返回 2。
inspect_global_config() {
  local dir="$1" top=""
  [[ -f "$GLOBAL_CONFIG" ]] || return 2
  have_python3 || return 2
  top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true)"
  python3 - "$GLOBAL_CONFIG" "$dir" "$top" <<'PY'
import json, os, sys
path, d, top = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    sys.exit(2)
def norm(p):
    return os.path.realpath(p).rstrip("/") or "/"
projects = cfg.get("projects") or {}
trusted = {norm(p) for p, v in projects.items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}
home = norm(os.path.expanduser("~"))
cands = []
for start in (d, top):
    if not start:
        continue
    cur = norm(start)
    while True:
        cands.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
ok = any(c in trusted and c not in (home, "/") for c in cands)
consent = cfg.get("remoteDialogSeen") is True
print("trusted=%s consent=%s" % ("yes" if ok else "no", "yes" if consent else "no"))
PY
}

# settings.json 的 env 块会被 LaunchAgent 读到（shell 里 export 的反而不会），单独扫一遍
settings_env_vars() {
  [[ -f "$SETTINGS" ]] || return 0
  have_python3 || return 0
  python3 - "$SETTINGS" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
except Exception:
    sys.exit(0)
env = cfg.get("env") or {}
bad = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
       "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_TELEMETRY",
       "DO_NOT_TRACK", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_GROWTHBOOK"]
print(" ".join(k for k in bad if k in env))
PY
}

find_claude() {
  # 官方安装器装的那份最稳定，优先；nvm/fnm 管理的路径可能会变
  if [[ -x "$HOME/.local/bin/claude" ]]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
  else
    CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
  fi
  if [[ -z "$CLAUDE_BIN" ]]; then
    local c
    for c in /opt/homebrew/bin/claude /usr/local/bin/claude; do
      if [[ -x "$c" ]]; then CLAUDE_BIN="$c"; break; fi
    done
  fi
  [[ -n "$CLAUDE_BIN" ]] || die "找不到 claude 命令。先安装：curl -fsSL https://claude.ai/install.sh | bash"
  local target
  target="$(readlink "$CLAUDE_BIN" 2>/dev/null || true)"
  case "$CLAUDE_BIN $target" in
    *fnm_multishells*|*/.nvm/*|*/tmp/*)
      warn "这个 claude 来自 nvm/fnm 管理的 node，路径以后可能失效。建议用官方安装器另装一份: curl -fsSL https://claude.ai/install.sh | bash" ;;
  esac
  PATH_ENV="$(dirname "$CLAUDE_BIN"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
}

update_hint() {
  case "$CLAUDE_BIN" in
    /opt/homebrew/*|/usr/local/Cellar/*) printf 'brew upgrade claude-code' ;;
    *) printf 'claude update' ;;
  esac
}

# 用 LaunchAgent 将来看到的那套环境去问一次 claude：Remote Control 到底可不可用
probe_eligibility() {
  local -a envargs
  envargs=(HOME="$HOME" PATH="$PATH_ENV" LANG=en_US.UTF-8)
  if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then envargs+=(CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR"); fi
  local out
  out="$(env -i "${envargs[@]}" "$CLAUDE_BIN" remote-control --help 2>&1 || true)"
  if [[ -z "$out" ]] || grep -qiE '^[[:space:]]*(error|✗)' <<<"$out"; then
    if (( DRY_RUN )); then
      warn "Remote Control 可用性探测未通过（dry-run 继续）: $(printf '%s' "$out" | head -n 2)"
    else
      die "Remote Control 在这台机器上不可用，claude 的原话：
$out"
    fi
  else
    ok "Remote Control 可用"
  fi
}

preflight() {
  log "体检"

  if (( ! IS_MAC )); then
    if (( DRY_RUN )); then
      warn "当前不是 macOS，只做 dry-run 演示"
    else
      die "这个脚本只能在 macOS 上运行（依赖 launchd）"
    fi
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    if (( DRY_RUN )); then
      warn "当前是 root 用户，真实安装时会拒绝（LaunchAgent 必须装在你自己的用户下）"
    else
      die "请不要用 sudo 运行：LaunchAgent 必须装在你自己的用户下"
    fi
  fi
  if (( IS_MAC )) && (( ! DRY_RUN )); then
    if [[ "$(launchctl managername 2>/dev/null || true)" != "Aqua" ]]; then
      die "请在 Mac 的图形界面里打开「终端」运行本脚本，不要通过 SSH：LaunchAgent 要装进登录会话，钥匙串也只在登录会话里可用"
    fi
  fi
  if (( IS_MAC )) && ! have_python3; then
    die "缺少 Xcode 命令行工具（python3/git 还是安装桩）。先运行: xcode-select --install ，装完再来"
  fi

  find_claude
  ok "claude: $CLAUDE_BIN"

  local ver
  ver="$("$CLAUDE_BIN" --version 2>/dev/null | awk 'NR==1{print $1}' || true)"
  [[ -n "$ver" ]] || die "无法读取 claude 版本（claude --version 没有输出）"
  version_ge "$ver" "$MIN_VERSION" || die "claude 版本 $ver 太旧，建议 ≥ $MIN_VERSION。运行: $(update_hint)"
  ok "版本: $ver"

  local status
  status="$("$CLAUDE_BIN" auth status --json 2>/dev/null || true)"
  if ! grep -Eq '"loggedIn":[[:space:]]*true' <<<"$status"; then
    die "claude 还没登录 claude.ai 账号。先运行: claude auth login"
  fi
  if grep -Eq '"apiProvider":' <<<"$status" && ! grep -Eq '"apiProvider":[[:space:]]*"firstParty"' <<<"$status"; then
    die "当前走的不是 Anthropic 官方 API（Bedrock / Vertex / 网关），Remote Control 不可用"
  fi
  ok "已登录 claude.ai"

  local v
  for v in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX \
           CLAUDE_CODE_USE_FOUNDRY CLAUDE_CODE_OAUTH_TOKEN DISABLE_TELEMETRY DO_NOT_TRACK \
           CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC DISABLE_GROWTHBOOK; do
    if [[ -n "${!v:-}" ]]; then
      warn "shell 里设置了 $v，它会让你在终端手动开的会话用不了 Remote Control（后台服务不继承 shell 变量，不受影响）"
    fi
  done
  local bad
  bad="$(settings_env_vars || true)"
  if [[ -n "$bad" ]]; then
    warn "$SETTINGS 的 env 块里有: $bad —— 这些会被后台服务读到，可能让 Remote Control 不可用；下一步的探测会给出结论"
  fi

  probe_eligibility

  [[ -d "$PROJECT_DIR" ]] || die "目录不存在: $PROJECT_DIR"
  PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
  if [[ "$PROJECT_DIR" == "$HOME" || "$PROJECT_DIR" == "/" ]]; then
    die "不能用家目录或根目录当工作目录（Claude 从不信任它们）。cd 到一个项目目录再运行，或加 --dir <路径>"
  fi
  case "$PROJECT_DIR" in
    "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*|"$HOME/Library/Mobile Documents"*|/Volumes/*)
      warn "这个目录受 macOS 隐私保护（桌面/文稿/下载/iCloud/外接盘）。后台服务第一次访问它时会在 Mac 屏幕上弹授权框，没人点就卡住。"
      warn "建议把项目放到 ~/code 之类的目录；或在 系统设置 → 隐私与安全性 → 完全磁盘访问权限 里加入 claude" ;;
  esac

  if (( SKIP_TRUST_CHECK )); then
    warn "已跳过目录信任 / 首次确认检测"
  else
    local info="" rc=0 trusted consent
    info="$(inspect_global_config "$PROJECT_DIR")" || rc=$?
    if (( rc != 0 )); then
      warn "读不到 $GLOBAL_CONFIG，无法判断目录信任 / 首次确认状态，继续"
    else
      trusted="${info#trusted=}"; trusted="${trusted%% *}"
      consent="${info##*consent=}"
      if [[ "$trusted" != "yes" || "$consent" != "yes" ]]; then
        die "还差一步手动确认（目录信任: $trusted，Remote Control 首次确认: $consent）。请先在终端里运行一次：

    cd \"$PROJECT_DIR\" && claude remote-control

  · 出现  Enable Remote Control? (y/n)  时输入 y
  · 如果弹出目录信任提示，选 Yes
  · 看到会话链接后按 Ctrl+C 退出

然后重新运行本脚本。这一步不能省：后台服务没有键盘，卡在这个提问上就会一直重启。
（确定都做过、只是检测不准：加 --skip-trust-check）"
      fi
    fi
  fi
  ok "工作目录: $PROJECT_DIR"

  if (( IS_MAC )); then
    local kc
    kc="$(security show-keychain-info "$HOME/Library/Keychains/login.keychain-db" 2>&1 || true)"
    case "$kc" in
      *timeout=*|*lock-on-sleep*)
        warn "登录钥匙串设置了自动锁定。后台服务读取登录凭据时会在屏幕上弹解锁框，没人点就卡住。"
        warn "建议在「钥匙串访问」里取消 login 钥匙串的自动锁定（钥匙串访问 → 编辑 → 更改钥匙串 login 的设置）" ;;
    esac
  fi
}

enable_startup_setting() {
  log "打开 remoteControlAtStartup（本机手动开的 claude 会话也会自动出现在网页端）"
  if ! have_python3; then
    warn "没有可用的 python3，请手动在 $SETTINGS 里加入: \"remoteControlAtStartup\": true"
    return 0
  fi
  if (( DRY_RUN )); then
    printf '   [dry-run] 写入 %s: "remoteControlAtStartup": true\n' "$SETTINGS"
    return 0
  fi
  mkdir -p "$(dirname "$SETTINGS")"
  if [[ -s "$SETTINGS" ]]; then
    cp "$SETTINGS" "$SETTINGS.bak-$(date +%Y%m%d%H%M%S)"
  fi
  python3 - "$SETTINGS" <<'PY'
import json, os, sys
path = sys.argv[1]
data = {}
if os.path.exists(path):
    with open(path) as f:
        raw = f.read().strip()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            sys.stderr.write('  ! %s 不是合法 JSON，未改动；请手动加入 "remoteControlAtStartup": true\n' % path)
            sys.exit(0)
if not isinstance(data, dict):
    sys.stderr.write('  ! %s 顶层不是对象，未改动\n' % path)
    sys.exit(0)
if data.get("remoteControlAtStartup") is True:
    print("  ✓ 已经是 true，无需改动")
    sys.exit(0)
data["remoteControlAtStartup"] = True
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, path)
print("  ✓ 已写入 " + path)
PY
}

build_plist() {
  local name="${SESSION_NAME:-$(hostname -s 2>/dev/null || hostname)}"
  local perm_block="" conf_block=""
  if [[ -n "$PERMISSION_MODE" ]]; then
    perm_block="    <string>--permission-mode</string>
    <string>$(xml_escape "$PERMISSION_MODE")</string>"
  fi
  if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
    conf_block="    <key>CLAUDE_CONFIG_DIR</key>
    <string>$(xml_escape "$CLAUDE_CONFIG_DIR")</string>"
  fi
  PLIST_CONTENT="$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$CLAUDE_BIN")</string>
    <string>remote-control</string>
    <string>--name</string>
    <string>$(xml_escape "$name")</string>
$perm_block
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
$conf_block
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$LOG")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$LOG")</string>
</dict>
</plist>
EOF
)"
  # 没传 --permission-mode / CLAUDE_CONFIG_DIR 时会留下空行，去掉
  PLIST_CONTENT="$(printf '%s\n' "$PLIST_CONTENT" | sed '/^[[:space:]]*$/d')"
}

install_agent() {
  local uid
  uid="$(id -u)"
  log "安装 LaunchAgent: $PLIST"
  if (( DRY_RUN )); then
    printf '%s\n' "$PLIST_CONTENT" | sed 's/^/   | /'
    printf '   [dry-run] launchctl bootstrap gui/%s %s\n' "$uid" "$PLIST"
    return 0
  fi
  mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
  if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
    log "已有旧的服务在跑，先停掉"
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
    local i
    for i in $(seq 1 30); do
      launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1 || break
      sleep 1
    done
    if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
      die "旧服务 30 秒内没有退出。手动运行  launchctl bootout gui/$uid/$LABEL  之后重试"
    fi
  fi
  # 日志归档，保证接下来看到的链接一定是这次启动打印的
  if [[ -s "$LOG" ]]; then
    mv -f "$LOG" "$LOG.1"
  fi
  printf '%s\n' "$PLIST_CONTENT" > "$PLIST"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST" >/dev/null || die "生成的 plist 不合法（脚本 bug）。请把 $PLIST 的内容发给我"
  fi
  launchctl bootstrap "gui/$uid" "$PLIST" || die "launchctl bootstrap 失败（错误见上一行）"
  ok "服务已启动（登录后自启，退出后 30 秒内自动拉起）"
}

show_result() {
  log "等待会话上线（最多 60 秒）"
  local i url=""
  for i in $(seq 1 30); do
    sleep 2
    url="$(strip_ansi < "$LOG" 2>/dev/null | grep -Eo 'https://claude\.ai/code/[A-Za-z0-9_-]+' | tail -n 1 || true)"
    [[ -n "$url" ]] && break
  done
  if [[ -n "$url" ]]; then
    ok "会话已上线: $url"
  else
    warn "还没在日志里看到会话链接。过一会儿运行  bash $0 --status  再看；日志在 $LOG"
    warn "如果日志显示它在反复退出，先在终端里手动运行  claude remote-control  看它报什么错"
  fi
  cat <<NEXT

接下来：
  1. 在另一台电脑打开 https://claude.ai/code ，侧边栏里会出现这台 Mac 的会话（带 Remote Control 标记）
  2. 手机 Claude App 里同样能看到并操作它
  3. 在网页端可以让它读 ~/.claude/scheduled-tasks/ 下的文件来查看每个本地 routine 的 prompt，也可以改 prompt；
     暂停、改时间这类操作仍要在桌面 App 的 Routines 页面做

查看状态:  bash $0 --status
卸载:      bash $0 --uninstall
NEXT
}

check_sleep() {
  command -v pmset >/dev/null 2>&1 || return 0
  local s
  s="$(pmset -g 2>/dev/null | awk '$1=="sleep"{print $2; exit}')"
  if [[ -n "$s" && "$s" != "0" ]]; then
    warn "系统允许自动休眠（pmset sleep=$s，0 才表示不休眠）。休眠期间网页端看到的是离线。"
    warn "建议：系统设置 → 电池/节能 → 关掉自动休眠；或 Claude App 设置 → Desktop app → General → Keep computer awake（仅桌面 App 运行期间有效）"
  fi
}

status() {
  local uid
  uid="$(id -u)"
  if launchctl print "gui/$uid/$LABEL" 2>/dev/null | grep -E '^[[:space:]]*(state|pid|last exit code) ='; then
    :
  else
    echo "LaunchAgent 未加载（没安装，或已卸载）"
  fi
  if [[ -f "$LOG" ]]; then
    echo "--- 最近日志: $LOG ($(du -h "$LOG" | awk '{print $1}')) ---"
    strip_ansi < "$LOG" | grep -v '^[[:space:]]*$' | tail -n 40
    if strip_ansi < "$LOG" | grep -qiE 'login expired|run /login|not logged in|auth login'; then
      echo
      echo "  ! 日志里出现登录过期 / 未登录的提示。在这台 Mac 上运行  claude auth login  之后，再运行  bash $0  重装即可"
    fi
  else
    echo "(暂无日志)"
  fi
}

uninstall() {
  local uid
  uid="$(id -u)"
  launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  ok "已卸载 LaunchAgent（$SETTINGS 里的 remoteControlAtStartup 保留，不需要的话手动改成 false）"
}

main() {
  case "$ACTION" in
    status)    status ;;
    uninstall) uninstall ;;
    install)
      preflight
      enable_startup_setting
      build_plist
      install_agent
      if (( ! DRY_RUN )); then show_result; fi
      check_sleep
      ;;
  esac
}

main
