#!/usr/bin/env bash
#
# setup-remote-control.sh  (Linux)
# 让这台常开的 Linux 主机可以从 claude.ai/code（网页）和 Claude 手机 App 远程操控。
#
# 做三件事：
#   1. 体检：claude CLI、版本、登录、Remote Control 可用性、目录信任、首次确认、systemd 用户实例
#   2. 在 settings.json 打开 remoteControlAtStartup —— 本机手动开的 claude 会话也自动出现在网页端
#   3. 安装一个 systemd --user 服务，常驻运行 `claude remote-control`（开机自启、退出后 30 秒内自动拉起）
#
# 第一次用之前，必须在项目目录里手动运行一次  claude remote-control  完成 y/n 确认（脚本会检测并提示）。
#
# 用法（在一个你用 claude 打开过的项目目录里执行；SSH 登录后运行即可，不需要图形界面）：
#   bash setup-remote-control.sh                 # 安装并启动
#   bash setup-remote-control.sh --status        # 看运行状态和最近日志
#   bash setup-remote-control.sh --uninstall     # 卸载服务
#   bash setup-remote-control.sh --dry-run       # 只打印将要做的事，不改任何东西
#
# 可选参数：
#   --dir <路径>              用哪个目录当会话的工作目录（默认当前目录）
#   --name <名字>             网页端看到的会话名（默认主机名）
#   --permission-mode <模式>  会话的权限模式（default acceptEdits plan auto dontAsk bypassPermissions manual），默认不传
#   --skip-trust-check        跳过"目录信任 / 首次确认"的检测
#
set -euo pipefail

LABEL="claude-remote-control"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/$LABEL.service"
LOG_DIR="$HOME/.local/state/claude-remote-control"
LOG="$LOG_DIR/claude-remote-control.log"
CONF_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CONF_DIR/settings.json"
if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
  GLOBAL_CONFIG="$CLAUDE_CONFIG_DIR/.claude.json"
else
  GLOBAL_CONFIG="$HOME/.claude.json"
fi
MIN_VERSION="2.1.200"
ME="$(id -un)"

PROJECT_DIR="$PWD"
SESSION_NAME=""
PERMISSION_MODE=""
ACTION="install"
DRY_RUN=0
SKIP_TRUST_CHECK=0
IS_LINUX=0
CLAUDE_BIN=""
PATH_ENV=""
SCRIPT_BIN=""
UNIT_CONTENT=""

if [[ "$(uname -s)" == "Linux" ]]; then IS_LINUX=1; fi

usage() {
  cat <<'USAGE'
用法: bash setup-remote-control.sh [--dir 路径] [--name 名字] [--permission-mode 模式]
                                   [--skip-trust-check] [--dry-run] [--status] [--uninstall]

在一个你用 claude 打开过的项目目录里运行（SSH 登录即可）。
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

# 会话名会写进 systemd 单元文件的引号里：引号、反斜杠、%（systemd 说明符）、$ 和换行都不行
case "$SESSION_NAME" in
  *\'*|*\"*|*\\*|*%*|*\$*|*$'\n'*) die "--name 里不能有引号、反斜杠、%、\$ 或换行" ;;
esac

have_python3() {
  python3 -c 'import json, os, sys' >/dev/null 2>&1
}

# version_ge A B  →  A >= B 时返回 0（只比较前三段数字）
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
# CLI 从当前目录往上找已信任的目录，到 git 仓库根目录为止；不在仓库里才会一直找到 /
cands = []
cur = norm(d)
stop = norm(top) if top else None
while True:
    cands.append(cur)
    if stop is not None and cur == stop:
        break
    parent = os.path.dirname(cur)
    if parent == cur:
        break
    cur = parent
ok = any(c in trusted and c not in (home, "/") for c in cands)
consent = cfg.get("remoteDialogSeen") is True
print("trusted=%s consent=%s" % ("yes" if ok else "no", "yes" if consent else "no"))
PY
}

# settings.json 的 env 块会被服务读到（shell 里 export 的反而不会），单独扫一遍
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
    for c in /usr/local/bin/claude /usr/bin/claude; do
      if [[ -x "$c" ]]; then CLAUDE_BIN="$c"; break; fi
    done
  fi
  [[ -n "$CLAUDE_BIN" ]] || die "找不到 claude 命令。先安装：curl -fsSL https://claude.ai/install.sh | bash"
  local target
  target="$(readlink -f "$CLAUDE_BIN" 2>/dev/null || true)"
  case "$CLAUDE_BIN $target" in
    *fnm_multishells*|*/.nvm/*|*/tmp/*)
      warn "这个 claude 来自 nvm/fnm 管理的 node，路径以后可能失效。建议用官方安装器另装一份: curl -fsSL https://claude.ai/install.sh | bash" ;;
  esac
  PATH_ENV="$(dirname "$CLAUDE_BIN"):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
}

update_hint() {
  printf 'claude update'
}

# 用服务将来看到的那套环境去问一次 claude：Remote Control 到底可不可用
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

# systemd --user 在 SSH 会话里需要 XDG_RUNTIME_DIR；pam_systemd 一般会设好，没设就补上
ensure_user_systemd() {
  command -v systemctl >/dev/null 2>&1 || die "找不到 systemctl：这个脚本依赖 systemd（Ubuntu/Debian/Fedora/Arch 都自带）"
  if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  fi
  local state
  state="$(systemctl --user is-system-running 2>/dev/null || true)"
  case "$state" in
    running|degraded|starting|initializing) ok "systemd 用户实例: $state" ;;
    *)
      if (( DRY_RUN )); then
        warn "systemd 用户实例不可用（状态: ${state:-无}），dry-run 继续"
      else
        die "systemd 用户实例不可用（状态: ${state:-无}）。通常是因为这个用户还没开 linger。先运行：

    sudo loginctl enable-linger $ME

然后重新登录一次再运行本脚本。"
      fi ;;
  esac
}

preflight() {
  log "体检"

  if (( ! IS_LINUX )); then
    if (( DRY_RUN )); then
      warn "当前不是 Linux，只做 dry-run 演示"
    else
      die "这个脚本只能在 Linux 上运行（依赖 systemd）。macOS 请用 mac/setup-remote-control.sh，Windows 请看 windows/"
    fi
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    if (( DRY_RUN )); then
      warn "当前是 root 用户，真实安装时会拒绝（服务必须装在你自己的用户下）"
    else
      die "请不要用 sudo 运行：服务必须装在你自己的用户下（claude 的登录也在这个用户下）"
    fi
  fi
  have_python3 || die "缺少 python3（脚本用它读写 JSON）。Debian/Ubuntu: sudo apt install python3"

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
    die "claude 还没登录 claude.ai 账号。先运行: claude auth login（SSH 里会给一个网址，浏览器登录后把 code 粘回来）"
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
      warn "shell 里设置了 $v，它会让你在终端手动开的会话用不了 Remote Control。服务用 /bin/sh 启动、不读 .zshenv / config.fish，但 ~/.config/environment.d/ 里的变量它会读到，请一并检查"
    fi
  done
  local bad
  bad="$(settings_env_vars || true)"
  if [[ -n "$bad" ]]; then
    warn "$SETTINGS 的 env 块里有: $bad —— 这些会被服务读到，可能让 Remote Control 不可用；下一步的探测会给出结论"
  fi

  probe_eligibility
  ensure_user_systemd

  SCRIPT_BIN="$(command -v script 2>/dev/null || true)"
  if [[ -n "$SCRIPT_BIN" ]] && ! "$SCRIPT_BIN" --version 2>/dev/null | grep -q util-linux; then
    SCRIPT_BIN=""   # busybox 的 script 没有 -e，会直接启动失败
  fi
  if [[ -n "$SCRIPT_BIN" ]]; then
    ok "会通过 $SCRIPT_BIN 给服务分配一个伪终端（claude 在没有终端时可能拒绝启动）"
  else
    warn "找不到 script 命令（util-linux），服务将直接运行 claude；如果日志显示它因为没有终端而退出，安装 util-linux 后重跑本脚本"
  fi

  [[ -d "$PROJECT_DIR" ]] || die "目录不存在: $PROJECT_DIR"
  PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
  if [[ "$PROJECT_DIR" == "$HOME" || "$PROJECT_DIR" == "/" ]]; then
    die "不能用家目录或根目录当工作目录（Claude 从不信任它们）。cd 到一个项目目录再运行，或加 --dir <路径>"
  fi

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
}

enable_startup_setting() {
  log "打开 remoteControlAtStartup（本机手动开的 claude 会话也会自动出现在网页端）"
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

build_unit() {
  local name="$SESSION_NAME"
  if [[ -z "$name" ]]; then
    name="${HOSTNAME:-$(uname -n)}"; name="${name%%.*}"   # 有的发行版没有 hostname 命令
  fi
  # systemd 单元文件里 % 是说明符，路径里出现要写成 %%
  local claude_esc="${CLAUDE_BIN//%/%%}" dir_esc="${PROJECT_DIR//%/%%}" log_esc="${LOG//%/%%}"
  local path_esc="${PATH_ENV//%/%%}" home_esc="${HOME//%/%%}"
  local cmd="'$claude_esc' remote-control --name '$name'"
  if [[ -n "$PERMISSION_MODE" ]]; then
    cmd="$cmd --permission-mode $PERMISSION_MODE"
  fi
  local exec_line
  if [[ -n "$SCRIPT_BIN" ]]; then
    # script -qfec "<命令>" /dev/null：给 claude 一个伪终端，输出照常进日志，退出码透传
    exec_line="$SCRIPT_BIN -qfec \"$cmd\" /dev/null"
  else
    exec_line="$cmd"
  fi
  local conf_line=""
  if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
    conf_line="Environment=\"CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR//%/%%}\""
  fi
  UNIT_CONTENT="$(cat <<EOT
[Unit]
Description=Claude Code Remote Control ($name)
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$dir_esc
Environment="PATH=$path_esc"
Environment="HOME=$home_esc"
Environment=LANG=en_US.UTF-8
Environment=SHELL=/bin/sh
$conf_line
ExecStart=$exec_line
Restart=always
RestartSec=30
StandardOutput=append:$log_esc
StandardError=append:$log_esc

[Install]
WantedBy=default.target
EOT
)"
  UNIT_CONTENT="$(printf '%s\n' "$UNIT_CONTENT" | sed '/^[[:space:]]*$/d; s/^\[Service\]$/\n[Service]/; s/^\[Install\]$/\n[Install]/')"
}

install_service() {
  log "安装 systemd 用户服务: $UNIT"
  if (( DRY_RUN )); then
    printf '%s\n' "$UNIT_CONTENT" | sed 's/^/   | /'
    printf '   [dry-run] systemctl --user daemon-reload && systemctl --user enable --now %s\n' "$LABEL"
    printf '   [dry-run] loginctl enable-linger %s\n' "$ME"
    return 0
  fi
  mkdir -p "$UNIT_DIR" "$LOG_DIR"
  if systemctl --user is-active --quiet "$LABEL" 2>/dev/null; then
    log "已有旧的服务在跑，先停掉"
    systemctl --user stop "$LABEL" || true
  fi
  # 日志归档，保证接下来看到的链接一定是这次启动打印的
  if [[ -s "$LOG" ]]; then
    mv -f "$LOG" "$LOG.1"
  fi
  printf '%s\n' "$UNIT_CONTENT" > "$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable --now "$LABEL" || die "systemctl --user enable --now $LABEL 失败（错误见上一行）"
  ok "服务已启动（退出后 30 秒内自动拉起）"

  # 没有 linger 的话，用户实例会在最后一个登录会话结束时被杀掉，服务也跟着没了
  if loginctl show-user "$ME" 2>/dev/null | grep -q '^Linger=yes'; then
    ok "开机自启已就绪（linger 已开启）"
  elif loginctl --no-ask-password enable-linger "$ME" 2>/dev/null; then
    ok "已为 $ME 开启 linger：开机后不用登录服务就会启动"
  elif [[ -t 0 ]] && command -v sudo >/dev/null 2>&1 \
       && { log "开启 linger 需要管理员权限，下面会要求输入 sudo 密码"; sudo loginctl enable-linger "$ME"; }; then
    ok "已为 $ME 开启 linger：开机后不用登录服务就会启动"
  else
    warn "无法自动开启 linger。请运行一次：  sudo loginctl enable-linger $ME  —— 否则你退出登录 / 重启后服务不会自己起来"
  fi
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
  1. 在你的电脑上用登录了这台主机账号的浏览器打开 https://claude.ai/code ，侧边栏里会出现这台主机的会话（带 Remote Control 标记）
  2. 手机 Claude App 里同样能看到并操作它
  3. 需要在终端里做的事（/resume、/plugin、重新登录）用 SSH 上来做

查看状态:  bash $0 --status
卸载:      bash $0 --uninstall
NEXT
}

status() {
  if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  fi
  if [[ -f "$UNIT" ]]; then
    systemctl --user --no-pager --lines=0 status "$LABEL" 2>&1 | sed -n '1,6p' || true
    echo "linger: $(loginctl show-user "$ME" 2>/dev/null | awk -F= '$1=="Linger"{print $2}')"
  else
    echo "服务未安装（没有 $UNIT）"
  fi
  if [[ -f "$LOG" ]]; then
    echo "--- 最近日志: $LOG ($(du -h "$LOG" | awk '{print $1}')) ---"
    strip_ansi < "$LOG" | grep -v '^[[:space:]]*$' | tail -n 40 || true
    if strip_ansi < "$LOG" | grep -qiE 'login expired|run /login|not logged in|auth login'; then
      echo
      echo "  ! 日志里出现登录过期 / 未登录的提示。在这台主机上运行  claude auth login  之后，再运行  systemctl --user restart $LABEL  即可"
    fi
  else
    echo "(暂无日志)"
  fi
}

uninstall() {
  if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  fi
  systemctl --user disable --now "$LABEL" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  ok "已卸载服务（$SETTINGS 里的 remoteControlAtStartup 保留，不需要的话手动改成 false；linger 也保留）"
}

main() {
  case "$ACTION" in
    status)    status ;;
    uninstall) uninstall ;;
    install)
      preflight
      enable_startup_setting
      build_unit
      install_service
      if (( ! DRY_RUN )); then show_result; fi
      ;;
  esac
}

main
