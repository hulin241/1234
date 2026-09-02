# 从一台电脑操控三台常开的 Claude Code 主机

三台常开主机各自登录着**不同的 claude.ai 账号**，人在另一台电脑前。目标：不传屏幕、不装 ToDesk 之类的远程桌面，在浏览器里直接操控三台主机上的 Claude Code。

## 思路

- Claude Code 是终端程序，操控它只需要传文字。远程桌面把整个屏幕编成视频再传，卡顿就来自这里。
- 官方的 [Remote Control](https://code.claude.com/docs/en/remote-control) 把主机上的会话原样同步到 claude.ai/code 和 Claude 手机 App：发消息、批准权限、看 diff、切模型都在网页里做，代码执行和文件仍留在主机上。主机只对外发 HTTPS，不开任何入站端口。
- Remote Control 按账号隔离：主机上用哪个账号登录，会话就只出现在哪个账号的网页里。所以你的电脑上开三个浏览器配置文件，各登录一个账号，三个标签页对应三台主机。
- 兜底：每台主机开 SSH。需要在终端里做的事（重新登录、`/resume`、装插件）走 SSH，不需要远程桌面。

本仓库提供三个系统的一键安装脚本，把 `claude remote-control` 做成常驻服务：

| 主机系统 | 脚本 | 常驻方式 |
|---|---|---|
| macOS | [`mac/setup-remote-control.sh`](mac/README.md) | LaunchAgent，登录后自启 |
| Linux | [`linux/setup-remote-control.sh`](linux/README.md) | systemd 用户服务，开机自启 |
| Windows | [`windows/setup-remote-control.ps1`](windows/README.md) | 任务计划程序，登录后自启 |

三个脚本参数一致：`--name` 会话名、`--dir` 工作目录、`--permission-mode` 权限模式、`--status` 看状态、`--uninstall` 卸载、`--dry-run` 只看不改（Windows 写成 `-Name` `-Dir` 等）。

## 第一步：每台主机装常驻服务

每台主机都要做一遍，用**这台主机自己的**账号。

1. **装 Claude Code CLI**，版本 ≥ 2.1.200：
   - macOS / Linux：`curl -fsSL https://claude.ai/install.sh | bash`
   - Windows PowerShell：`irm https://claude.ai/install.ps1 | iex`
   - 已装的话 `claude update`
2. **登录**：`claude auth login`，选 claude.ai 账号。API key 和 `claude setup-token` 生成的长期 token 都不能用于 Remote Control。通过 SSH 登录时终端会给一个网址，浏览器里登录后把 code 粘回终端。
   需要 Pro / Max 套餐；Team / Enterprise 要管理员先在 claude.ai/admin-settings/claude-code 打开 Remote Control。
3. **检查环境**：shell 和 `~/.claude/settings.json` 的 `env` 块里都不要有 `ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY`、`CLAUDE_CODE_OAUTH_TOKEN`、`DISABLE_TELEMETRY`、`DO_NOT_TRACK`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`、`DISABLE_GROWTHBOOK`，任何一个都会让 Remote Control 不可用。`claude doctor` 会直接告诉你哪一项没过。
4. **选一个项目目录**（不能是家目录），**手动跑一次**：
   ```bash
   cd ~/code/某个项目
   claude remote-control
   ```
   出现 `Enable Remote Control? (y/n)` 输 `y`，弹出目录信任提示选 Yes，看到会话链接后 Ctrl+C 退出。这一步不能省：后台服务没有键盘，卡在提问上会一直重启。
5. **装服务**（脚本会先体检，再写配置、再启动）：
   ```bash
   git clone https://github.com/hulin241/1234.git ~/claude-remote-control
   cd ~/code/某个项目

   # macOS（要在 Mac 的图形界面终端里跑，不能走 SSH）
   bash ~/claude-remote-control/mac/setup-remote-control.sh --name 主机A --permission-mode acceptEdits

   # Linux（SSH 里跑即可）
   bash ~/claude-remote-control/linux/setup-remote-control.sh --name 主机B --permission-mode acceptEdits
   ```
   ```powershell
   # Windows（PowerShell，不需要管理员）
   git clone https://github.com/hulin241/1234.git $HOME\claude-remote-control
   cd C:\code\某个项目
   powershell -ExecutionPolicy Bypass -File $HOME\claude-remote-control\windows\setup-remote-control.ps1 -Name 主机C -PermissionMode acceptEdits
   ```
6. **别让主机休眠**。休眠期间网页里看到的是离线。各系统的做法见对应目录的 README。

关于 `--permission-mode acceptEdits`：这是官方文档给远程场景的例子。改文件不用逐个批准，执行命令仍会弹权限提示，你在网页或手机上点允许即可。网页端自己只能在 Manual / Accept edits / Plan 之间切换，`auto` 和 `bypassPermissions` 只能在主机上设，常开的主机不建议开：拿到这个账号密码的人就等于拿到了这台主机的执行权限。三个账号都开二步验证。

## 第二步：你的电脑

**浏览器**

- Chrome / Edge：右上角头像 → 添加 → 新建配置文件，建三个，各登录一个账号，各打开 https://claude.ai/code 并固定成标签页。
- Firefox：装 Multi-Account Containers 扩展，三个容器各登录一个账号。
- 侧边栏里带绿点的电脑图标就是那台主机的会话，名字就是 `--name` 给的名字。点进去就是完整会话：发消息、批准权限、在 diff 面板看改动、切模型和 effort、停掉子任务、传图片和文件。
- `/model sonnet`、`/effort high`、`/compact`、`/clear`、`/rename` 这类命令在网页里可用；`/resume`、`/plugin` 只能在主机终端里用。

**手机**

- Claude App 的 Code 页同样能看到并操作这些会话。手机一次只登一个账号，切账号要重新登录。
- 在主机会话里运行 `/config`，打开 **Push when actions required**，需要你批准时手机会收到推送。

## 第三步：SSH 兜底

强烈建议做，花不了几分钟：

- **开 SSH**：macOS 系统设置 → 通用 → 共享 → 远程登录；Linux 装 `openssh-server`；Windows 设置 → 可选功能 → OpenSSH 服务器。只用密钥登录，关掉密码登录。
- **没有公网 IP**：三台主机和你的电脑都装 [Tailscale](https://tailscale.com/)，之后直接 `ssh 主机A`，不用端口转发。
- **在终端里跑 Claude 且断线不丢会话**：用 tmux。`~/.tmux.conf` 加官方给的三行，否则 Shift+Enter 和通知失效：
  ```
  set -g allow-passthrough on
  set -s extended-keys on
  set -as terminal-features 'xterm*:extkeys'
  ```
  然后 `ssh 主机A -t 'tmux new -A -s claude'`，进去后运行 `claude`。
- **Remote Control 的另一种跑法**：不装服务，在 tmux 里运行 `claude --remote-control 主机A`。这是交互模式，本地能打字、网页也能操作，断网时会无限重试；服务模式 `claude remote-control` 断网约 10 分钟会退出，靠服务自动拉起。两种任选一种，不要同时跑。

## 日常与故障

- **网页里显示离线**：主机休眠了，或者 `claude remote-control` 进程退出了。用各目录里的 `--status` 看服务状态和日志。
- **断网**：服务模式断网约 10 分钟自动退出，服务会在 30 秒内拉起。4 小时内重启能找回原来的会话，超过就是新会话，旧的还在列表里。
- **登录过期**：网页里的会话不再响应或显示离线，日志里有 `login expired`。SSH 上主机运行 `claude auth login`，**务必用这台主机原来的账号**，登错账号会开一个没有历史的新会话。然后重启服务：macOS 重跑一遍安装脚本，Linux `systemctl --user restart claude-remote-control`，Windows 重跑一遍安装脚本。
- **三台主机之间**：跨会话消息只在同一账号内有效，三个账号之间不能互发。需要协调时你在浏览器里分别说。
- **Windows**：官方文档没有明确写 Remote Control 支持原生 Windows。第一次手动运行 `claude remote-control` 就是测试；不行的话装 WSL2 在里面用 Linux 脚本，或者在 Claude 桌面 App 里打开 Settings → Claude Code → Enable remote control by default 并保持一个会话开着。
- **本地 routine**：桌面 App 的本地 routine 不会出现在网页端，见 [mac/README.md](mac/README.md)。

## 参考

- [Remote Control](https://code.claude.com/docs/en/remote-control)
- [Mobile](https://code.claude.com/docs/en/mobile)
- [Permission modes](https://code.claude.com/docs/en/permission-modes)
- [Configure tmux](https://code.claude.com/docs/en/terminal-config#configure-tmux)
- [Cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)
