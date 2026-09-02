#!/usr/bin/env bash
#
# setup-remote-control.sh
# 让这台常开的 Mac 可以从 claude.ai/code（网页）和 Claude 手机 App 远程操控。
#
# 做三件事：
#   1. 体检：claude CLI 是否存在、版本够不够、有没有登录 claude.ai、当前目录是否已被 Claude 信任
#   2. 在 ~/.claude/settings.json 打开 remoteControlAtStartup —— 本机手动开的 claude 会话也自动出现在网页端
#   3. 安装一个 LaunchAgent，常驻运行 `claude remote-control`（开机自启、意外退出 30 秒内自动拉起）
#
# 用法（在一个你用 claude 打开过的项目目录里执行）：
#   bash setup-remote-control.sh                 # 安装并启动
#   bash setup-remote-control.sh --status        # 看运行状态和最近日志
#   bash setup-remote-control.sh --uninstall     # 卸载 LaunchAgent
#   bash setup-remote-control.sh --dry-run       # 只打印将要做的事，不改任何东西
#
# 可选参数：
#   --dir <路径>              用哪个目录当会话的工作目录（默认当前目录）
#   --name <名字>             网页端看到的会话名（默认主机名）
#   --permission-mode <模式>  会话的权限模式，如 acceptEdits（默认不传，用 claude 自己的默认值）
#   --skip-trust-check        跳过"目录是否已信任"的检测
#
set -euo pipefail

LABEL="com.claude.remote-control"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/claude-remote-control.log"
SETTINGS="$HOME/.claude/settings.json"
MIN_VERSION="2.1.200"

PROJECT_DIR="$PWD"
SESSION_NAME=""
PERMISSION_MODE=""
ACTION="install"
DRY_RUN=0
SKIP_TRUST_CHECK=0
CLAUDE_BIN=""
PLIST_CONTENT=""

usage() {
  cat <<'USAGE'
用法: bash setup-remote-control.sh [--dir 路径] [--name 名字] [--permission-mode 模式]
                                   [--skip-trust-check] [--dry-run] [--status] [--uninstall]

在一个你用 claude 打开过的项目目录里运行。默认动作是安装并启动常驻的 Remote Control 服务。
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

# version_ge A B  →  A >= B 时返回 0（只比较前三段，兼容 macOS 自带的 bash 3.2）
version_ge() {
  local IFS=.
  local -a a b
  read -r -a a <<<"$1"
  read -r -a b <<<"$2"
  local i x y
  for i in 0 1 2; do
    x="${a[$i]:-0}"; y="${b[$i]:-0}"
    (( 10#$x > 10#$y )) && return 0
    (( 10#$x < 10#$y )) && return 1
  done
  return 0
}

# 用 sed 而不是 ${var//} 做替换：bash 5.2 起替换串里的 & 有特殊含义，会把 &lt; 变成 <lt;
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

# 返回值：0 已信任，1 未信任，2 无法判断
project_trusted() {
  local dir="$1"
  [[ -f "$HOME/.claude.json" ]] || return 1
  command -v python3 >/dev/null 2>&1 || return 2
  python3 - "$dir" <<'PY'
import json, os, sys
d = sys.argv[1]
try:
    with open(os.path.expanduser("~/.claude.json")) as f:
        cfg = json.load(f)
except Exception:
    sys.exit(2)
projects = cfg.get("projects") or {}
cands = {d, d.rstrip("/"), os.path.realpath(d)}
for p, v in projects.items():
    if p in cands or p.rstrip("/") in cands:
        if isinstance(v, dict) and v.get("hasTrustDialogAccepted"):
            sys.exit(0)
sys.exit(1)
PY
}

preflight() {
  log "体检"

  if [[ "$(uname -s)" != "Darwin" ]]; then
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

  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
  if [[ -z "$CLAUDE_BIN" ]]; then
    local c
    for c in "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
      if [[ -x "$c" ]]; then CLAUDE_BIN="$c"; break; fi
    done
  fi
  [[ -n "$CLAUDE_BIN" ]] || die "找不到 claude 命令。先安装：curl -fsSL https://claude.ai/install.sh | bash"
  ok "claude: $CLAUDE_BIN"

  local ver
  ver="$("$CLAUDE_BIN" --version 2>/dev/null | awk 'NR==1{print $1}')"
  [[ -n "$ver" ]] || die "无法读取 claude 版本（claude --version 没有输出）"
  version_ge "$ver" "$MIN_VERSION" || die "claude 版本 $ver 太旧，需要 ≥ $MIN_VERSION。运行: claude update"
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
           DISABLE_TELEMETRY DO_NOT_TRACK CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC DISABLE_GROWTHBOOK; do
    if [[ -n "${!v:-}" ]]; then
      warn "环境变量 $v 已设置，会让 Remote Control 不可用。LaunchAgent 不继承它，但你在终端手动开的会话会受影响"
    fi
  done

  [[ -d "$PROJECT_DIR" ]] || die "目录不存在: $PROJECT_DIR"
  PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
  if [[ "$PROJECT_DIR" == "$HOME" ]]; then
    die "不能用家目录当工作目录（Claude 从不信任家目录）。cd 到一个项目目录再运行，或加 --dir <路径>"
  fi
  if (( SKIP_TRUST_CHECK )); then
    warn "已跳过目录信任检测"
  else
    local rc=0
    project_trusted "$PROJECT_DIR" || rc=$?
    case "$rc" in
      0) ;;
      1) die "这个目录还没被 Claude 信任: $PROJECT_DIR
    先在这个目录里运行一次  claude  ，在信任提示里选 Yes，然后输入 /exit 退出，再重新运行本脚本。
    （确定已经信任过、只是检测不准的话，加 --skip-trust-check）" ;;
      *) warn "无法判断目录是否已信任（缺少 python3 或读不到 ~/.claude.json），继续" ;;
    esac
  fi
  ok "工作目录: $PROJECT_DIR"
}

enable_startup_setting() {
  log "打开 remoteControlAtStartup（本机手动开的 claude 会话也会自动出现在网页端）"
  if ! command -v python3 >/dev/null 2>&1; then
    warn "没有 python3，请手动在 $SETTINGS 里加入: \"remoteControlAtStartup\": true"
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
  local bindir
  bindir="$(dirname "$CLAUDE_BIN")"
  local path_env="$bindir:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  local perm_block=""
  if [[ -n "$PERMISSION_MODE" ]]; then
    perm_block="    <string>--permission-mode</string>
    <string>$(xml_escape "$PERMISSION_MODE")</string>"
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
    <string>$(xml_escape "$path_env")</string>
    <key>HOME</key>
    <string>$(xml_escape "$HOME")</string>
    <key>LANG</key>
    <string>en_US.UTF-8</string>
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
  # 没传 --permission-mode 时会留下一个空行，去掉
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
    sleep 2
  fi
  printf '%s\n' "$PLIST_CONTENT" > "$PLIST"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST" >/dev/null || die "生成的 plist 不合法（脚本 bug）。请把 $PLIST 的内容发给我"
  fi
  if ! launchctl bootstrap "gui/$uid" "$PLIST" 2>/dev/null; then
    # 老系统没有 bootstrap 子命令时退回 load
    launchctl load -w "$PLIST" || die "launchctl 加载失败。手动试试: launchctl bootstrap gui/$uid \"$PLIST\""
  fi
  launchctl kickstart -k "gui/$uid/$LABEL" 2>/dev/null || true
  ok "服务已启动（开机自启，退出后 30 秒内自动拉起）"
}

show_result() {
  log "等待会话上线（最多 30 秒）"
  local i url=""
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 2
    url="$(grep -Eo 'https://claude\.ai/code/[A-Za-z0-9_-]+' "$LOG" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$url" ]] && break
  done
  if [[ -n "$url" ]]; then
    ok "会话已上线: $url"
  else
    warn "还没在日志里看到会话链接。过一会儿运行  bash $0 --status  再看；日志在 $LOG"
  fi
  cat <<NEXT

接下来：
  1. 在另一台电脑打开 https://claude.ai/code ，侧边栏里会出现这台 Mac 的会话（带 Remote Control 标记）
  2. 手机 Claude App 里同样能看到并操作它
  3. 在网页端对它说「列出我本机的 scheduled tasks」就能看到这台机器上的 routine

查看状态:  bash $0 --status
卸载:      bash $0 --uninstall
NEXT
}

check_sleep() {
  command -v pmset >/dev/null 2>&1 || return 0
  local s
  s="$(pmset -g 2>/dev/null | awk '$1=="sleep"{print $2; exit}')"
  if [[ -n "$s" && "$s" != "0" ]]; then
    warn "系统会在无操作 $s 分钟后休眠，休眠期间网页端看到的是离线。"
    warn "建议：系统设置 → 电池/节能 → 关掉自动休眠；或 Claude App 设置 → General → Keep computer awake"
  fi
}

status() {
  local uid
  uid="$(id -u)"
  if launchctl print "gui/$uid/$LABEL" 2>/dev/null | grep -E '^\s*(state|pid|last exit code) ='; then
    :
  else
    echo "LaunchAgent 未加载（没安装，或已卸载）"
  fi
  echo "--- 最近日志: $LOG ---"
  tail -n 40 "$LOG" 2>/dev/null || echo "(暂无日志)"
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
