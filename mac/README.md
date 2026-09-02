# 把常开的 Mac 接到 claude.ai/code

## 问题

网页端 claude.ai/code 只显示**云端**的内容。桌面 App 里那批 routine（`Local` 类型）和本地会话都存在那台 Mac 上，由桌面 App 在运行期间按分钟检查并触发；网页端从设计上就看不到它们，不是同步坏了。

## 两种做法

### 做法 0：不用脚本（最简单）

在常开的 Mac 上打开 Claude 桌面 App：**Settings → Claude Code → Enable remote control by default**，然后在 Code 标签里**保持一个会话开着**。这个会话会出现在另一台电脑的 claude.ai/code 侧边栏和手机 App 里，你可以直接操作它。

限制：只有你在 App 里开着的会话才可见；App 退出、Mac 重启后要重新打开。

### 做法 1：本脚本（常驻、可从网页随时新开会话）

在 Mac 上跑一个常驻的 `claude remote-control` 服务。会话仍然在这台 Mac 上执行（文件、本地脚本全都可用），网页端可以随时新开会话；进程意外退出 30 秒内自动拉起；用户登录后自动启动。

`setup-remote-control.sh` 把这件事压缩成一条命令。下面的内容都是讲做法 1。

## 前提

- macOS，并且**在 Mac 的图形界面里登录着**（脚本要在本机的「终端」里跑，不能通过 SSH）
- Xcode 命令行工具：`xcode-select --install`（没装的话 python3 / git 只是会弹安装框的桩）
- Claude Code CLI：`claude --version` 能看到版本号，建议 ≥ 2.1.200。没装就 `curl -fsSL https://claude.ai/install.sh | bash`（只装了桌面 App 的人通常没有 CLI）
- 用 claude.ai 账号登录：`claude auth login`。`claude setup-token` 生成的长期 token 和 API key 都不能用于 Remote Control
- Pro / Max / Team / Enterprise 套餐（Team/Enterprise 需要管理员在 claude.ai/admin-settings/claude-code 打开 Remote Control）

## 第一次：手动跑一次（不能省）

`claude remote-control` 第一次运行会问一句 `Enable Remote Control? (y/n)`，还可能弹目录信任提示。后台服务没有键盘，卡在这些提问上就会一直重启，所以要先在终端里手动过一遍：

```bash
cd ~/某个项目目录          # 不能是家目录
claude remote-control
```

- 出现 `Enable Remote Control? (y/n)` 时输入 `y`
- 弹出目录信任提示时选 Yes
- 看到会话链接后按 Ctrl+C 退出

脚本会检测这两项有没有完成，没完成会停下来提示你。

## 安装

```bash
git clone -b claude/routine-visibility-issue-ajcz8j https://github.com/hulin241/1234.git ~/claude-remote-control
cd ~/刚才那个项目目录
bash ~/claude-remote-control/mac/setup-remote-control.sh
```

脚本会：

1. 体检：CLI、版本、登录、Remote Control 可用性（用后台服务将来的环境真实探测一次）、目录信任、首次确认
2. 在 `~/.claude/settings.json` 写入 `"remoteControlAtStartup": true`（改之前备份一份 `.bak-时间戳`）
3. 归档旧日志，安装 `~/Library/LaunchAgents/com.claude.remote-control.plist`，常驻运行 `claude remote-control`
4. 等会话上线，打印它在 claude.ai/code 的链接

想先看看它会做什么、不动任何东西：

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --dry-run
```

## 装完之后

- 另一台电脑打开 https://claude.ai/code ，侧边栏里出现这台 Mac 的会话（带 Remote Control 标记）
- 手机 Claude App 里同样能看到
- 你在这台 Mac 上手动开的 `claude` 会话，也会自动出现在网页端（`remoteControlAtStartup`）
- 关于本地 routine，从网页端能做的是：让它读 `~/.claude/scheduled-tasks/<名字>/SKILL.md` 查看每个 routine 的 prompt，也可以直接改这个文件（下次运行生效）。**暂停、改时间、改目录、改模型仍要在桌面 App 的 Routines 页面做** —— 这些不在文件里，CLI 会话也没有桌面 App 那套管理工具

## 常用命令

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --status      # 状态 + 最近日志（已去掉控制字符）
bash ~/claude-remote-control/mac/setup-remote-control.sh --uninstall   # 卸载常驻服务
```

日志在 `~/Library/Logs/claude-remote-control.log`，每次重新安装会把旧的挪到 `.log.1`。

## 可选参数

| 参数 | 作用 |
|---|---|
| `--dir <路径>` | 指定工作目录（默认当前目录） |
| `--name <名字>` | 网页端看到的会话名（默认主机名） |
| `--permission-mode <模式>` | 会话权限模式：default / acceptEdits / plan / auto / dontAsk / bypassPermissions / manual；默认不传 |
| `--skip-trust-check` | 跳过目录信任 / 首次确认检测 |

## 想要"完全不问权限"

网页端和手机 App 的模式下拉框只有 **Accept edits / Plan / Auto** 三项，没有 Bypass permissions —— 这是云端会话的硬限制，[官方文档](https://code.claude.com/docs/en/permission-modes)写得很明确：云端会话不提供 bypass，而且**会话还会忽略设置文件里的 `defaultMode: "bypassPermissions"` 和 `"dontAsk"`**，仓库里提交 `.claude/settings.json` 对云端会话没有任何作用（静默忽略）。

真正能做到不问权限的只有本机会话，也就是这个方案：

```bash
# 1) 先在 Mac 的终端里手动接受一次免责弹窗（后台服务没有键盘，跳过这步会一直重启）
cd ~/code/你的项目 && claude --dangerously-skip-permissions
#    出现警告对话框时选接受，然后 Ctrl+C 退出。接受结果会写进 ~/.claude/settings.json，只需一次

# 2) 再装常驻服务
bash ~/claude-remote-control/mac/setup-remote-control.sh --permission-mode bypassPermissions
```

两个反直觉的地方：

- **下拉框仍然不会显示 Bypass permissions**。会话从不把这个模式上报给 claude.ai，所以网页端显示的是别的模式，但实际不会弹权限请求。代价是：一旦你在 App 里动了那个下拉框，就切出了 bypass，而且**没法从 App 切回来** —— 只能去 Mac 的终端里按 `Shift+Tab`，或者重跑上面的脚本。
- **`--dangerously-skip-permissions` 拒绝以 root/sudo 运行**，脚本本身也拒绝 sudo，这两点是一致的。

安全提醒：官方建议只在隔离容器 / 虚拟机里用 bypass。这里是一台常开、能读写你整个家目录、还能从手机操控的 Mac，正好是文档警告的相反情形。折中方案是 `--permission-mode auto`：由分类器在后台判断，日常几乎不打断，危险动作仍会拦。

## 注意

- **是"登录后自启"，不是"开机自启"**。LaunchAgent 装在登录会话里，Mac 重启后要有人登录一次才会启动。要做到真正无人值守：系统设置 → 用户与群组 → 自动登录 选这个用户（开了 FileVault 时没有这个选项），并把 Claude App 加入登录项。
- **Mac 必须保持唤醒并联网**。休眠期间网页端看到的是"离线"。系统设置 → 电池/节能 里关掉自动休眠；或 Claude App 设置 → Desktop app → General → **Keep computer awake**（只在桌面 App 运行期间有效）。合上盖子仍会休眠。
- **项目别放在 桌面/文稿/下载/iCloud/外接盘**。这些目录受 macOS 隐私保护，后台服务第一次访问会在屏幕上弹授权框，没人点就卡住。放到 `~/code` 之类的目录最省事；或在 系统设置 → 隐私与安全性 → 完全磁盘访问权限 里加入 claude。
- **登录钥匙串不要设自动锁定**。后台服务读取登录凭据时会弹解锁框。脚本会检测并提示。
- **claude 的登录会过期**。过期后远程会话停止响应，网页端看不出原因。在这台 Mac 上重新 `claude auth login`，然后重跑一次脚本。`--status` 会在日志里发现相关提示时提醒你。
- 网络断超过约 10 分钟，`claude remote-control` 会自行退出；LaunchAgent 会在 30 秒内把它拉起来。
- **本地 routine 只在桌面 App 运行、且 Mac 未休眠时执行**。这个方案让你从网页操作那台 Mac，但 routine 本身仍是本地的，claude.ai/code/routines 页面不会列出它们。要让某个 routine 出现在那个页面并且电脑关机也照跑，得在 App 里用 **New routine → Cloud** 重建成云端 routine —— 云端跑在全新容器里，碰不到本机文件，依赖本机脚本的 routine 需要先改造。

## 卸载

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --uninstall
# 如果也不想让手动会话自动上网页：
# 把 ~/.claude/settings.json 里的 "remoteControlAtStartup" 改成 false 或删掉
```

## 参考

- [Remote Control](https://code.claude.com/docs/en/remote-control)
- [Desktop scheduled tasks（本地 routine）](https://code.claude.com/docs/en/desktop-scheduled-tasks)
- [Routines（云端 routine）](https://code.claude.com/docs/en/routines)
- [Desktop app](https://code.claude.com/docs/en/desktop)
- [跨会话消息](https://code.claude.com/docs/en/cross-session-messaging)
