# 把常开的 Windows 主机接到 claude.ai/code

在 Windows 主机上注册一个任务计划程序任务，登录后在隐藏窗口里常驻运行 `claude remote-control`。会话在这台主机上执行，网页端和手机 App 可以随时操作；进程退出 30 秒内自动拉起。

`setup-remote-control.ps1` 把这件事压缩成一条命令。整体思路和多主机、多账号的用法见[根目录 README](../README.md)。

> 官方文档没有明确写 Remote Control 支持原生 Windows（macOS / Linux / WSL 是明确支持的）。下面「第一次手动跑一次」这一步同时也是在验证它能不能用。跑不起来的话见文末的两个替代方案。

## 前提

- Windows 10 1809+ / Windows 11，普通用户即可，不需要管理员
- Claude Code CLI ≥ 2.1.200：PowerShell 里 `irm https://claude.ai/install.ps1 | iex`。装 [Git for Windows](https://git-scm.com/downloads/win) 可以让 Claude 用 Bash 工具，可选
- 用 claude.ai 账号登录：`claude auth login`。`claude setup-token` 的长期 token 和 API key 都不能用于 Remote Control
- Pro / Max / Team / Enterprise 套餐（Team / Enterprise 需要管理员打开 Remote Control）
- 系统环境变量里不要有 `ANTHROPIC_BASE_URL`、`ANTHROPIC_API_KEY`、`DISABLE_TELEMETRY`、`DO_NOT_TRACK` 之类（任务计划程序启动的服务会继承系统环境变量）

## 第一次：手动跑一次（不能省）

```powershell
cd C:\code\某个项目          # 不能是用户目录
claude remote-control
```

- 出现 `Enable Remote Control? (y/n)` 时输入 `y`
- 弹出目录信任提示时选 Yes
- 看到会话链接后按 Ctrl+C 退出

## 安装

```powershell
git clone https://github.com/hulin241/1234.git $HOME\claude-remote-control
cd C:\code\某个项目
powershell -ExecutionPolicy Bypass -File $HOME\claude-remote-control\windows\setup-remote-control.ps1 -Name 主机C -PermissionMode acceptEdits
```

脚本会：

1. 体检：CLI、版本、登录、Remote Control 可用性、系统环境变量、目录信任、首次确认
2. 在 `%USERPROFILE%\.claude\settings.json` 写入 `"remoteControlAtStartup": true`（改之前备份一份 `.bak-时间戳`）
3. 生成 `%LOCALAPPDATA%\claude-remote-control\run.ps1`，注册任务「Claude Remote Control」：当前用户登录时启动、隐藏窗口、不限运行时长
4. 立即启动任务，确认 `claude remote-control` 进程在跑

先看看它会做什么、不动任何东西：

```powershell
powershell -ExecutionPolicy Bypass -File $HOME\claude-remote-control\windows\setup-remote-control.ps1 -DryRun
```

## 装完之后

- 用登录了这台主机账号的浏览器打开 https://claude.ai/code ，侧边栏里按名字找这台主机的会话
- 别让电脑休眠：以管理员身份运行 `powercfg /change standby-timeout-ac 0`
- 这是**登录后自启**，不是开机自启。重启后要有人登录一次；要真正无人值守，用 `netplwiz` 开自动登录，并把这台电脑放在安全的地方

## 常用命令

```powershell
powershell -ExecutionPolicy Bypass -File $HOME\claude-remote-control\windows\setup-remote-control.ps1 -Status      # 状态 + 最近日志
powershell -ExecutionPolicy Bypass -File $HOME\claude-remote-control\windows\setup-remote-control.ps1 -Uninstall   # 卸载任务
```

日志 `%LOCALAPPDATA%\claude-remote-control\claude-remote-control.log` 只记录每次启动和退出的时间。claude 自己的输出没有写进文件，是为了给它保留一个控制台，避免它因为没有终端而拒绝启动。要看它报什么错，在 PowerShell 里手动运行 `claude remote-control`。

## 可选参数

| 参数 | 作用 |
|---|---|
| `-Dir <路径>` | 指定工作目录（默认当前目录） |
| `-Name <名字>` | 网页端看到的会话名（默认计算机名） |
| `-PermissionMode <模式>` | 会话权限模式：default / acceptEdits / plan / auto / dontAsk / bypassPermissions / manual；默认不传 |
| `-SkipTrustCheck` | 跳过目录信任 / 首次确认检测 |

## 注意

- **claude 的登录会过期**。过期后远程会话停止响应。在这台主机上重新 `claude auth login`（用原来的账号），然后重跑一遍安装脚本。
- **网络断超过约 10 分钟**，`claude remote-control` 会自行退出；任务里的循环会在 30 秒后把它拉起来。4 小时内重启能找回原会话。
- 日志里如果每隔 30 秒就有一次「退出」，说明它启动不了，先手动运行看原因。

## 原生 Windows 跑不起来时的替代方案

1. **WSL2**：装 Ubuntu，在里面 `/etc/wsl.conf` 加 `[boot]` `systemd=true`，`wsl --shutdown` 后重进，然后按 [linux/README.md](../linux/README.md) 操作。另外注册一个登录时运行 `wsl.exe -d Ubuntu` 的任务让 WSL 保持运行。
2. **桌面 App**：装 Claude 桌面 App，Settings → Claude Code → **Enable remote control by default**，在 Code 标签里保持一个会话开着。App 退出、电脑重启后要重新打开。

## 卸载

```powershell
powershell -ExecutionPolicy Bypass -File $HOME\claude-remote-control\windows\setup-remote-control.ps1 -Uninstall
# 如果也不想让手动会话自动上网页：
# 把 %USERPROFILE%\.claude\settings.json 里的 "remoteControlAtStartup" 改成 false 或删掉
```
