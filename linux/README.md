# 把常开的 Linux 主机接到 claude.ai/code

在 Linux 主机上跑一个常驻的 `claude remote-control` 服务。会话在这台主机上执行（文件、本地脚本全都可用），网页端和手机 App 可以随时操作；进程退出 30 秒内自动拉起；开机自启，不需要有人登录。不需要图形界面，纯服务器也行。

`setup-remote-control.sh` 把这件事压缩成一条命令。整体思路和多主机、多账号的用法见[根目录 README](../README.md)。

## 前提

- 有 systemd 的发行版（Ubuntu / Debian / Fedora / Arch 等都是），并且是**普通用户**，不要用 root 或 sudo
- `python3`（脚本用它读写 JSON）
- Claude Code CLI ≥ 2.1.200：`curl -fsSL https://claude.ai/install.sh | bash`
- 用 claude.ai 账号登录：`claude auth login`。SSH 里会给一个网址，浏览器登录后把 code 粘回终端。`claude setup-token` 的长期 token 和 API key 都不能用于 Remote Control
- Pro / Max / Team / Enterprise 套餐（Team / Enterprise 需要管理员打开 Remote Control）

## 第一次：手动跑一次（不能省）

```bash
cd ~/code/某个项目          # 不能是家目录
claude remote-control
```

- 出现 `Enable Remote Control? (y/n)` 时输入 `y`
- 弹出目录信任提示时选 Yes
- 看到会话链接后按 Ctrl+C 退出

## 安装

```bash
git clone https://github.com/hulin241/1234.git ~/claude-remote-control
cd ~/code/某个项目
bash ~/claude-remote-control/linux/setup-remote-control.sh --name 主机B --permission-mode acceptEdits
```

脚本会：

1. 体检：CLI、版本、登录、Remote Control 可用性（用一个干净的环境真实探测一次）、systemd 用户实例、目录信任、首次确认
2. 在 `~/.claude/settings.json` 写入 `"remoteControlAtStartup": true`（改之前备份一份 `.bak-时间戳`）
3. 写 `~/.config/systemd/user/claude-remote-control.service` 并 `enable --now`；同时 `loginctl enable-linger`，让服务开机就起、不依赖登录
4. 等会话上线，打印它在 claude.ai/code 的链接

服务通过 `script` 给 claude 分配一个伪终端再运行，避免它在没有终端时拒绝启动；输出追加到 `~/.local/state/claude-remote-control/claude-remote-control.log`。服务用 `/bin/sh` 启动，不会读 `.zshenv` / `config.fish`，但 `~/.config/environment.d/` 里的变量会被读到，别在那里放 `ANTHROPIC_BASE_URL` 之类的变量。

先看看它会做什么、不动任何东西：

```bash
bash ~/claude-remote-control/linux/setup-remote-control.sh --dry-run
```

## 常用命令

```bash
bash ~/claude-remote-control/linux/setup-remote-control.sh --status      # 状态 + 最近日志
bash ~/claude-remote-control/linux/setup-remote-control.sh --uninstall   # 卸载服务
systemctl --user restart claude-remote-control                           # 重启（比如重新登录之后）
```

## 可选参数

| 参数 | 作用 |
|---|---|
| `--dir <路径>` | 指定工作目录（默认当前目录） |
| `--name <名字>` | 网页端看到的会话名（默认主机名） |
| `--permission-mode <模式>` | 会话权限模式：default / acceptEdits / plan / auto / dontAsk / bypassPermissions / manual；默认不传 |
| `--skip-trust-check` | 跳过目录信任 / 首次确认检测 |

## 注意

- **linger**：脚本会尝试 `loginctl enable-linger 你的用户名`。多数系统这一步需要管理员权限，脚本会提示并要求输入 sudo 密码；没有 sudo 的话它会告诉你之后手动跑一次。没有 linger 的话，你退出 SSH 或重启后服务不会自己起来。
- **别让主机休眠**。台式机 / 服务器一般不会自动休眠；笔记本改成常开的话：`sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`，合盖不休眠改 `/etc/systemd/logind.conf` 里的 `HandleLidSwitch=ignore`。
- **claude 的登录会过期**。过期后远程会话停止响应，`--status` 会在日志里发现相关提示。SSH 上来重新 `claude auth login`（用这台主机原来的账号），再 `systemctl --user restart claude-remote-control`。
- **网络断超过约 10 分钟**，`claude remote-control` 会自行退出；服务会在 30 秒内把它拉起来。4 小时内重启能找回原会话。
- **日志会一直追加**。每次重新运行安装命令都会把旧日志挪到 `.log.1`，想控制大小就隔段时间重跑一次安装命令。
- 如果日志显示服务反复退出，先在终端里手动运行 `claude remote-control` 看它报什么错。

## 卸载

```bash
bash ~/claude-remote-control/linux/setup-remote-control.sh --uninstall
# 如果也不想让手动会话自动上网页：
# 把 ~/.claude/settings.json 里的 "remoteControlAtStartup" 改成 false 或删掉
# 如果也不要 linger 了：loginctl disable-linger
```
