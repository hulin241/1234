#!/usr/bin/env bash
#
# claude-history.sh
# 把这台机器上「本地的聊天记录」翻出来：列出、看、搜、导出、拉起（继续聊）。
#
# 为什么需要它：claude.ai/code 网页端只列云端会话；Remote Control 让网页能操作这台
# Mac 的**当前**会话，但**过去**的会话（不管是终端里开的、桌面 App 里开的，还是本地
# routine 跑出来的）只存在这台机器的硬盘上，网页端不会列出来。这个脚本就是在这台机器
# 上（自己在终端里跑，或者让远程会话替你跑）把它们读出来。
#
# 只读：默认不改 ~/.claude 里的任何东西，只有 export --out 会写你指定的文件。
#
# 用法：
#   bash claude-history.sh list                    # 列最近的会话
#   bash claude-history.sh list --dir ~/code/x     # 只看某个项目
#   bash claude-history.sh show <会话ID前缀|last>  # 打印一次完整对话
#   bash claude-history.sh search "关键词"          # 全文搜所有会话
#   bash claude-history.sh export <会话ID> --out a.md
#   bash claude-history.sh resume <会话ID>         # 打印「接着聊」要敲的命令
#   bash claude-history.sh live                    # 现在正开着的会话
#   bash claude-history.sh where                   # 记录存在哪、占多大、保留多久
#
set -euo pipefail

CONF_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
PROJECTS_DIR="$CONF_DIR/projects"
SETTINGS="$CONF_DIR/settings.json"
PROMPT_HISTORY="$CONF_DIR/history.jsonl"

MODE=""
TARGET=""
FILTER_DIR=""
LIMIT="30"
OUT_FILE=""
SHOW_TOOLS="0"
SHOW_THINKING="0"
SHOW_RAW="0"
WANT_JSON="0"

usage() {
  cat <<'USAGE'
用法: bash claude-history.sh <命令> [参数]

命令:
  list                      列出本机所有会话（按最后活动时间倒序）
  show <ID|last>            打印一次完整对话（Markdown）
  search <关键词>            在所有会话的正文里搜关键词
  export <ID|last> --out F  把一次对话写成 Markdown 文件
  resume <ID|last>          打印「接着聊」/「后台拉起」要敲的命令
  prompts [关键词]           列出你打过的每一句 prompt（history.jsonl，不受 30 天清理影响）
  live                      列出此刻正在运行的会话（含 App / Remote Control 开的）
  where                     记录存在哪个目录、占多大、本地保留多少天

参数:
  --dir <路径>    只看这个项目目录下的会话（list / search）
  --limit <N>     最多显示几条（list 默认 30，search 默认 30；给 0 表示不限）
  --out <文件>    export 的输出路径
  --tools         show/export/search 时也带上工具调用
  --thinking      show/export 时也带上 thinking 块
  --raw           show/export 时不隐藏 system-reminder 之类的注入内容
  --json          list / live 输出 JSON，方便脚本处理
  -h, --help      显示本帮助

ID 可以只写前几位（≥4 位），也可以写 last —— 表示最近活动的那个会话。
USAGE
}

die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }

need_arg() { [[ -n "${2:-}" ]] || die "参数 $1 后面需要一个值"; }

have_python3() { python3 -c 'import json, os, sys' >/dev/null 2>&1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)   usage; exit 0 ;;
    --dir)       need_arg "$1" "${2:-}"; FILTER_DIR="$2"; shift 2 ;;
    --limit)     need_arg "$1" "${2:-}"; LIMIT="$2"; shift 2 ;;
    --out)       need_arg "$1" "${2:-}"; OUT_FILE="$2"; shift 2 ;;
    --tools)     SHOW_TOOLS=1; shift ;;
    --thinking)  SHOW_THINKING=1; shift ;;
    --raw)       SHOW_RAW=1; shift ;;
    --json)      WANT_JSON=1; shift ;;
    -*)          usage >&2; die "未知参数: $1" ;;
    *)
      if [[ -z "$MODE" ]]; then MODE="$1"
      elif [[ -z "$TARGET" ]]; then TARGET="$1"
      else die "多余的参数: $1（关键词里有空格的话用引号括起来）"; fi
      shift ;;
  esac
done

[[ "$LIMIT" =~ ^[0-9]+$ ]] || die "--limit 要是数字: $LIMIT"

case "$MODE" in
  ""|help) usage; exit 0 ;;
  list|show|search|export|resume|where|live|prompts) ;;
  *) usage >&2; die "未知命令: $MODE" ;;
esac

have_python3 || die "缺少可用的 python3（macOS 上先运行: xcode-select --install）"

if [[ ! -d "$PROJECTS_DIR" && "$MODE" != "live" && "$MODE" != "prompts" ]]; then
  die "找不到 $PROJECTS_DIR —— 这台机器上还没有本地会话记录（或者设了 CLAUDE_CONFIG_DIR 指向别处）"
fi

case "$MODE" in
  show|export|resume) [[ -n "$TARGET" ]] || die "$MODE 需要一个会话 ID（或 last）。先用 list 看有哪些" ;;
  search)             [[ -n "$TARGET" ]] || die "search 需要一个关键词" ;;
esac

# live 走 CLI：claude agents --json 是官方给脚本用的接口，能同时看到交互式和后台会话
live_json() {
  local bin=""
  if [[ -x "$HOME/.local/bin/claude" ]]; then bin="$HOME/.local/bin/claude"
  else bin="$(command -v claude 2>/dev/null || true)"; fi
  [[ -n "$bin" ]] || return 1
  "$bin" agents --json --all 2>/dev/null || return 1
}

LIVE=""
if [[ "$MODE" == "live" || "$MODE" == "list" ]]; then
  LIVE="$(live_json || true)"
  if [[ "$MODE" == "live" && -z "$LIVE" ]]; then
    die "读不到正在运行的会话（claude 命令不在 PATH 里，或者这台机器上没有 claude CLI）"
  fi
fi

python3 - "$MODE" "$PROJECTS_DIR" "$TARGET" "$FILTER_DIR" "$LIMIT" "$OUT_FILE" \
            "$SHOW_TOOLS" "$SHOW_THINKING" "$SHOW_RAW" "$WANT_JSON" "$SETTINGS" \
            "$PROMPT_HISTORY" "$LIVE" <<'PY'
import calendar, json, os, signal, sys, time

(mode, projects_dir, target, filter_dir, limit_s, out_file,
 show_tools_s, show_thinking_s, show_raw_s, want_json_s,
 settings_path, prompt_history_path, live_raw) = sys.argv[1:14]

limit = int(limit_s)
show_tools = show_tools_s == "1"
show_thinking = show_thinking_s == "1"
show_raw = show_raw_s == "1"
want_json = want_json_s == "1"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    # 让 `... | head` 之类的管道提前关闭时安静退出，而不是抛 BrokenPipeError
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass

RED, DIM, BOLD, GREEN, RESET = "\033[1;31m", "\033[2m", "\033[1m", "\033[1;32m", "\033[0m"
if not sys.stdout.isatty():
    RED = DIM = BOLD = GREEN = RESET = ""


def die(msg):
    sys.stderr.write("  %s\n" % msg)
    sys.exit(1)


def fmt_time(ts):
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def parse_ts(s):
    # 记录里是 ISO8601 UTC，例如 2026-09-02T17:41:56.297Z；timegm 按 UTC 解释，不受本地 DST 影响
    if not isinstance(s, str) or not s:
        return 0.0
    head = s.split(".")[0].rstrip("Z")
    try:
        return float(calendar.timegm(time.strptime(head, "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0


def human_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "%dB" % n


def text_of(content):
    """把 message.content 拍平成 (正文, [thinking], [(工具名, 摘要)])"""
    if isinstance(content, str):
        return content, [], []
    parts, thinking, tools = [], [], []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text") or "")
            elif t == "thinking":
                thinking.append(b.get("thinking") or "")
            elif t == "tool_use":
                inp = b.get("input") or {}
                summary = ""
                for k in ("command", "file_path", "pattern", "path", "url", "prompt", "description"):
                    if isinstance(inp.get(k), str):
                        summary = inp[k]
                        break
                if not summary:
                    try:
                        summary = json.dumps(inp, ensure_ascii=False)
                    except Exception:
                        summary = str(inp)
                tools.append((b.get("name") or "?", summary))
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, str):
                    tools.append(("(结果)", c))
                elif isinstance(c, list):
                    for cb in c:
                        if isinstance(cb, dict) and cb.get("type") == "text":
                            tools.append(("(结果)", cb.get("text") or ""))
    return "\n".join(p for p in parts if p), thinking, tools


def oneline(s, n=100):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def session_files(root):
    if not os.path.isdir(root):
        return
    for proj in sorted(os.listdir(root)):
        pdir = os.path.join(root, proj)
        if not os.path.isdir(pdir):
            continue
        for name in sorted(os.listdir(pdir)):
            # <session>.jsonl 才是当前记录；.orphaned-* / .superseded-* 是被搁置的旧版本，
            # 官方文档说它们本来就不出现在 /resume 的列表里，这里也跳过
            if name.endswith(".jsonl") and ".orphaned-" not in name:
                yield os.path.join(pdir, name)


BIG_FILE = 50 * 1024 * 1024   # 超过这个大小就不整份解析了，用文件头 + mtime 估

INJECT_TAGS = ("system-reminder", "command-message", "command-name",
               "command-args", "local-command-stdout", "skill-format")


def strip_injected(text):
    """去掉 system-reminder 一类由工具注入的块 —— 它们不是你打的字。--raw 可关掉"""
    if show_raw or not text:
        return text
    out = text
    for tag in INJECT_TAGS:
        open_t, close_t = "<%s>" % tag, "</%s>" % tag
        while True:
            i = out.find(open_t)
            if i < 0:
                break
            j = out.find(close_t, i)
            if j < 0:
                out = out[:i]
                break
            out = out[:i] + out[j + len(close_t):]
    return out


def scan_meta(path):
    """整份扫一遍拿元信息 + 准确的消息条数；文件太大时退回只读文件头"""
    meta = {
        "path": path,
        "id": os.path.basename(path)[:-6],
        "cwd": "", "branch": "", "version": "", "entrypoint": "", "title": "",
        "mtime": 0.0, "size": 0, "start": 0.0, "end": 0.0,
        "messages": 0, "approx": False,
    }
    try:
        meta["mtime"] = os.path.getmtime(path)
        meta["size"] = os.path.getsize(path)
    except OSError:
        return None
    if meta["size"] == 0:
        return None
    partial = meta["size"] > BIG_FILE
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                for src, key in (("cwd", "cwd"), ("gitBranch", "branch"),
                                 ("version", "version"), ("entrypoint", "entrypoint")):
                    if not meta[key] and isinstance(o.get(src), str):
                        meta[key] = o[src]
                ts = parse_ts(o.get("timestamp"))
                if ts:
                    if not meta["start"]:
                        meta["start"] = ts
                    meta["end"] = ts
                if o.get("type") in ("user", "assistant") and not o.get("isSidechain"):
                    meta["messages"] += 1
                    if not meta["title"] and o.get("type") == "user":
                        body, _, _ = text_of((o.get("message") or {}).get("content"))
                        body = strip_injected(body)
                        if body.strip():
                            meta["title"] = oneline(body)
                if partial and i > 400 and meta["title"]:
                    meta["approx"] = True
                    break
    except (IOError, OSError):
        return None
    if meta["approx"]:
        try:
            with open(path, "rb") as f:
                meta["messages"] = sum(c.count(b"\n") for c in iter(lambda: f.read(1 << 20), b""))
        except (IOError, OSError):
            pass
    if not meta["end"]:
        meta["end"] = meta["mtime"]
    if meta["messages"] == 0:
        return None      # 开了没说话就退出的空会话，不值得列
    return meta


def load_sessions(root, filter_dir):
    out = []
    fd = os.path.realpath(os.path.expanduser(filter_dir)) if filter_dir else ""
    for path in session_files(root):
        m = scan_meta(path)
        if not m:
            continue
        if fd:
            cwd = os.path.realpath(m["cwd"]) if m["cwd"] else ""
            if not cwd or not (cwd == fd or cwd.startswith(fd + os.sep)):
                continue
        out.append(m)
    out.sort(key=lambda m: m["end"], reverse=True)
    return out


def live_map():
    if not live_raw.strip():
        return {}
    try:
        arr = json.loads(live_raw)
    except ValueError:
        return {}
    return {a.get("sessionId"): a for a in arr if isinstance(a, dict)}


def resolve(sessions, want):
    if want == "last":
        if not sessions:
            die("没有任何会话记录")
        return sessions[0]
    hits = [s for s in sessions if s["id"] == want]
    if not hits:
        if len(want) < 4:
            die("会话 ID 至少写 4 位: %s" % want)
        hits = [s for s in sessions if s["id"].startswith(want)]
    if not hits:
        die("找不到会话: %s（用 list 看有哪些）" % want)
    if len(hits) > 1:
        die("ID 前缀 %s 匹配到 %d 个会话，请多写几位：\n%s"
            % (want, len(hits), "\n".join("    " + h["id"] for h in hits[:10])))
    return hits[0]


def iter_records(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def render(meta, out):
    live = live_map().get(meta["id"])
    out.append("# 会话 %s" % meta["id"])
    out.append("")
    out.append("- 目录: %s" % (meta["cwd"] or "?"))
    if meta["branch"]:
        out.append("- 分支: %s" % meta["branch"])
    out.append("- 时间: %s → %s" % (fmt_time(meta["start"]), fmt_time(meta["end"])))
    out.append("- 来源: %s%s" % (meta["entrypoint"] or "?",
                                 ("，claude %s" % meta["version"]) if meta["version"] else ""))
    if live:
        out.append("- 状态: 正在运行（pid %s）" % live.get("pid"))
    out.append("- 文件: %s (%s)" % (meta["path"], human_size(meta["size"])))
    if not show_raw:
        out.append("- 说明: 已隐藏 system-reminder 等注入内容（想看全部加 --raw）")
    out.append("")

    last_role = None
    for o in iter_records(meta["path"]):
        if o.get("isSidechain"):
            continue
        typ = o.get("type")
        if typ not in ("user", "assistant"):
            continue
        msg = o.get("message") or {}
        body, thinking, tools = text_of(msg.get("content"))
        if typ == "user":
            body = strip_injected(body)
        stamp = time.strftime("%H:%M:%S", time.localtime(parse_ts(o.get("timestamp"))))
        who = "👤 你" if typ == "user" else "🤖 Claude"
        if typ == "user" and not body.strip() and not show_tools:
            continue  # 纯 tool_result 的 user 记录
        if body.strip():
            if last_role != (typ, stamp):
                out.append("## %s · %s" % (who, stamp))
                out.append("")
            out.append(body.rstrip())
            out.append("")
            last_role = (typ, stamp)
        if show_thinking and thinking:
            out.append("<details><summary>thinking</summary>")
            out.append("")
            for t in thinking:
                out.append(t.rstrip())
            out.append("")
            out.append("</details>")
            out.append("")
        if show_tools and tools:
            for name, summary in tools:
                out.append("- `%s` %s" % (name, oneline(summary, 160)))
            out.append("")
    return out


def cmd_list():
    sessions = load_sessions(projects_dir, filter_dir)
    live = live_map()
    if want_json:
        rows = []
        for s in sessions[: limit or None]:
            rows.append({
                "id": s["id"], "cwd": s["cwd"], "branch": s["branch"],
                "entrypoint": s["entrypoint"], "version": s["version"],
                "title": s["title"], "messages": s["messages"], "bytes": s["size"],
                "startedAt": int(s["start"]), "lastActivityAt": int(s["end"]),
                "running": s["id"] in live, "path": s["path"],
            })
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not sessions:
        if filter_dir:
            print("%s 下没有会话记录（去掉 --dir 看全部）" % filter_dir)
        else:
            print("这台机器上还没有本地会话记录（%s 是空的）" % projects_dir)
        return
    shown = sessions[: limit or None]
    for s in shown:
        mark = (GREEN + "●" + RESET) if s["id"] in live else " "
        print("%s %s%s%s  %s  %s%d条  %s  %s" % (
            mark, BOLD, s["id"][:8], RESET, fmt_time(s["end"]),
            "~" if s["approx"] else "", s["messages"],
            (s["entrypoint"] or "-"), s["cwd"] or "?"))
        if s["branch"]:
            print("   %s%s%s" % (DIM, s["branch"], RESET))
        if s["title"]:
            print("   %s" % s["title"])
        print("")
    print("%s共 %d 个会话，显示 %d 个。● = 此刻正在运行。%s"
          % (DIM, len(sessions), len(shown), RESET))
    print("%s看某一个: bash claude-history.sh show %s%s"
          % (DIM, shown[0]["id"][:8], RESET))


def cmd_show():
    sessions = load_sessions(projects_dir, "")
    meta = resolve(sessions, target)
    out = render(meta, [])
    print("\n".join(out))


def cmd_export():
    sessions = load_sessions(projects_dir, "")
    meta = resolve(sessions, target)
    out = render(meta, [])
    dest = out_file or ("claude-%s.md" % meta["id"][:8])
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("已写入 %s (%s)" % (dest, human_size(os.path.getsize(dest))))


def cmd_search():
    needle = target.lower()
    sessions = load_sessions(projects_dir, filter_dir)
    hits = 0
    cap = limit or 10 ** 9
    for s in sessions:
        for o in iter_records(s["path"]):
            if o.get("type") not in ("user", "assistant"):
                continue
            if o.get("isSidechain"):
                continue
            body, _, tools = text_of((o.get("message") or {}).get("content"))
            if o.get("type") == "user":
                body = strip_injected(body)
            blobs = [body] + ([t[1] for t in tools] if show_tools else [])
            for blob in blobs:
                if not blob:
                    continue
                low = blob.lower()
                pos = low.find(needle)
                if pos < 0:
                    continue
                start = max(0, pos - 50)
                snippet = oneline(blob[start:pos + len(needle) + 60], 160)
                who = "你" if o.get("type") == "user" else "Claude"
                stamp = fmt_time(parse_ts(o.get("timestamp")))
                print("%s%s%s  %s  %s  %s" % (BOLD, s["id"][:8], RESET, stamp, who, s["cwd"] or "?"))
                print("   %s" % snippet)
                print("")
                hits += 1
                break
            if hits >= cap:
                break
        if hits >= cap:
            break
    if hits == 0:
        print("没搜到 %r。（默认只搜对话正文，加 --tools 连工具调用一起搜）" % target)
    else:
        print("%s%d 条匹配%s%s" % (DIM, hits, "（到上限了，--limit 0 看全部）" if hits >= cap else "", RESET))


def cmd_resume():
    sessions = load_sessions(projects_dir, "")
    meta = resolve(sessions, target)
    cwd = meta["cwd"] or "<目录未知>"
    live = live_map().get(meta["id"])
    print("会话 %s" % meta["id"])
    print("  目录: %s" % cwd)
    print("  最后活动: %s" % fmt_time(meta["end"]))
    if meta["title"]:
        print("  开头: %s" % meta["title"])
    print("")
    if live:
        print("  ! 这个会话此刻正开着（pid %s）。想接着聊就去那个窗口，"
              "或者加 --fork-session 另开一份副本。" % live.get("pid"))
        print("")
    print("在这台机器的终端里接着聊：")
    print("  cd %s && claude --resume %s" % (cwd, meta["id"]))
    print("")
    print("从网页 / 远程会话里后台拉起（不需要终端，拉起后用 claude logs <短id> 看输出）：")
    print("  cd %s && claude --bg --resume %s \"接着上面的活儿继续\"" % (cwd, meta["id"]))
    print("")
    print("不想动原来的记录，就另开一份副本：加 --fork-session")


def cmd_live():
    arr = json.loads(live_raw) if live_raw.strip() else []
    if want_json:
        print(json.dumps(arr, ensure_ascii=False, indent=2))
        return
    if not arr:
        print("此刻没有正在运行的会话。")
        return
    for a in arr:
        started = a.get("startedAt")
        started = fmt_time(started / 1000.0) if isinstance(started, (int, float)) else "?"
        print("%s%s%s  %s  %s  %s" % (
            BOLD, (a.get("sessionId") or "?")[:8], RESET, started,
            a.get("kind") or "?", a.get("cwd") or "?"))
        name = a.get("name")
        if name:
            print("   名字: %s   pid: %s" % (name, a.get("pid")))
        print("")
    print("%s共 %d 个。看它的对话: bash claude-history.sh show %s%s"
          % (DIM, len(arr), (arr[0].get("sessionId") or "")[:8], RESET))


def cmd_prompts():
    """~/.claude/history.jsonl：你打过的每一句话。它不在 30 天清理范围内，
    所以就算 transcript 被清掉了，这里通常还留着。"""
    if not os.path.exists(prompt_history_path):
        print("没有 %s —— 这台机器上还没记下 prompt 历史" % prompt_history_path)
        return
    needle = target.lower() if target else ""
    rows = []
    with open(prompt_history_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            text = ""
            for k in ("display", "prompt", "text", "content", "input"):
                if isinstance(o.get(k), str) and o[k].strip():
                    text = o[k]
                    break
            if not text:
                continue
            proj = ""
            for k in ("project", "cwd", "projectPath", "dir"):
                if isinstance(o.get(k), str):
                    proj = o[k]
                    break
            ts = 0.0
            raw_ts = o.get("timestamp") or o.get("time") or o.get("createdAt")
            if isinstance(raw_ts, (int, float)):
                ts = raw_ts / 1000.0 if raw_ts > 1e11 else float(raw_ts)
            elif isinstance(raw_ts, str):
                ts = parse_ts(raw_ts)
            if filter_dir:
                fd = os.path.realpath(os.path.expanduser(filter_dir))
                if not proj or not (os.path.realpath(proj) == fd
                                    or os.path.realpath(proj).startswith(fd + os.sep)):
                    continue
            if needle and needle not in text.lower():
                continue
            rows.append({"text": text, "project": proj, "at": ts})
    if not rows:
        print("没找到匹配的 prompt。" if needle else "%s 是空的。" % prompt_history_path)
        return
    rows.reverse()               # 文件里是从旧到新，倒过来先看最近的
    shown = rows[: limit or None]
    if want_json:
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return
    for r in shown:
        print("%s%s%s  %s" % (DIM, fmt_time(r["at"]) if r["at"] else "?", RESET, r["project"] or ""))
        print("  %s" % oneline(strip_injected(r["text"]), 200))
        print("")
    print("%s共 %d 条，显示 %d 条（--limit 0 看全部）%s" % (DIM, len(rows), len(shown), RESET))


def cmd_where():
    sessions = load_sessions(projects_dir, "")
    total = sum(s["size"] for s in sessions)
    projects = {}
    for s in sessions:
        projects[s["cwd"] or "?"] = projects.get(s["cwd"] or "?", 0) + 1
    orphaned = 0
    if os.path.isdir(projects_dir):
        for proj in os.listdir(projects_dir):
            pdir = os.path.join(projects_dir, proj)
            if not os.path.isdir(pdir):
                continue
            for name in os.listdir(pdir):
                if ".orphaned-" in name or ".superseded-" in name:
                    orphaned += 1

    print("聊天记录（transcript）: %s/<项目>/<会话ID>.jsonl" % projects_dir)
    print("  %d 个会话，合计 %s" % (len(sessions), human_size(total)))
    if sessions:
        print("  最早 %s   最近 %s"
              % (fmt_time(min(s["start"] or s["end"] for s in sessions)),
                 fmt_time(max(s["end"] for s in sessions))))
    if orphaned:
        print("  另有 %d 个 .orphaned / .superseded 的旧版本记录，本脚本不列（/resume 也不列）" % orphaned)
    print("")
    for cwd, n in sorted(projects.items(), key=lambda kv: -kv[1])[:15]:
        print("  %4d  %s" % (n, cwd))
    if len(projects) > 15:
        print("  ...  还有 %d 个项目" % (len(projects) - 15))
    print("")

    if os.path.exists(prompt_history_path):
        n = 0
        try:
            with open(prompt_history_path, "rb") as f:
                n = sum(c.count(b"\n") for c in iter(lambda: f.read(1 << 20), b""))
        except (IOError, OSError):
            pass
        print("打过的 prompt: %s（约 %d 条，不在自动清理范围内）" % (prompt_history_path, n))
    else:
        print("打过的 prompt: 还没有 %s" % prompt_history_path)
    print("")

    days = None
    desk = None
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        days = cfg.get("cleanupPeriodDays")
        desk = cfg.get("desktopSessionCleanupPeriodDays")
    except Exception:
        pass
    print("保留多久（官方文档 /docs/en/claude-directory）：")
    if days is None:
        print("  cleanupPeriodDays 没设 → 用默认值 30 天，超过就自动删")
        print("  想留久一点，在 %s 里加  \"cleanupPeriodDays\": 365" % settings_path)
    else:
        print("  cleanupPeriodDays = %s 天" % days)
    if desk is None:
        print("  桌面 App / Cowork 里开的会话默认不按天数删（要给它们上限就设 desktopSessionCleanupPeriodDays）")
    else:
        print("  desktopSessionCleanupPeriodDays = %s 天" % desk)
    print("")
    print("这些文件只在这台机器上。claude.ai/code 的列表里不会出现它们 ——")
    print("网页端能看到的办法是让远程会话在这台机器上跑这个脚本（见 mac/README.md）。")


{"list": cmd_list, "show": cmd_show, "search": cmd_search, "export": cmd_export,
 "resume": cmd_resume, "live": cmd_live, "where": cmd_where,
 "prompts": cmd_prompts}[mode]()
PY
