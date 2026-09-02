# 把常开的 Mac 接到 claude.ai/code

## 问题

网页端 claude.ai/code 只显示**云端**的内容。桌面 App 里那批 routine（`Local` 类型，由 launchd 触发）和本地会话都存在那台 Mac 上，网页端从设计上就看不到它们，不是同步坏了。

## 解法

在常开的 Mac 上跑一个 **Remote Control** 服务。会话仍然在这台 Mac 上执行（文件、launchd、本地脚本全都可用），但从任何一台电脑的 claude.ai/code、或手机 Claude App 都能看到并操作它。

`setup-remote-control.sh` 把这件事压缩成一条命令，并且做成开机自启、掉线自动拉起的常驻服务。

## 前提

- macOS，Claude Code CLI ≥ 2.1.200（`claude --version`）
- 已用 claude.ai 账号登录（`claude auth status`；没登录就 `claude auth login`）
- Pro / Max / Team / Enterprise 套餐（Team/Enterprise 需要管理员在 claude.ai/admin-settings/claude-code 打开 Remote Control）
- 一个你用 `claude` 打开过、并且点过"信任"的项目目录（家目录不行）

## 安装（在常开的那台 Mac 上）

```bash
git clone https://github.com/hulin241/1234.git ~/claude-remote-control
cd ~/某个用claude打开过的项目目录
bash ~/claude-remote-control/mac/setup-remote-control.sh
```

脚本会：

1. 体检：CLI、版本、登录、目录信任
2. 在 `~/.claude/settings.json` 写入 `"remoteControlAtStartup": true`（改之前会备份一份 `.bak-时间戳`）
3. 安装 `~/Library/LaunchAgents/com.claude.remote-control.plist`，常驻运行 `claude remote-control`
4. 等会话上线，打印它在 claude.ai/code 的链接

想先看看它会做什么、不动任何东西：

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --dry-run
```

## 装完之后

- 另一台电脑打开 https://claude.ai/code ，侧边栏里出现这台 Mac 的会话
- 手机 Claude App 里同样能看到
- 对它说「列出我本机的 scheduled tasks」「暂停 xxx」「把周报的 prompt 改成……」，它在本机执行
- 你在这台 Mac 上手动开的 `claude` 会话，也会自动出现在网页端（`remoteControlAtStartup`）

## 常用命令

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --status      # 状态 + 最近日志
bash ~/claude-remote-control/mac/setup-remote-control.sh --uninstall   # 卸载常驻服务
```

日志在 `~/Library/Logs/claude-remote-control.log`。

## 可选参数

| 参数 | 作用 |
|---|---|
| `--dir <路径>` | 指定工作目录（默认当前目录） |
| `--name <名字>` | 网页端看到的会话名（默认主机名） |
| `--permission-mode <模式>` | 会话权限模式，如 `acceptEdits`；默认不传 |
| `--skip-trust-check` | 跳过目录信任检测 |

## 注意

- **Mac 必须保持唤醒并联网**。休眠期间网页端看到的是"离线"。系统设置里关掉自动休眠，或在 Claude App 设置 → General 打开 **Keep computer awake**；合上盖子仍会休眠。
- 网络断超过约 10 分钟，`claude remote-control` 会自行退出；LaunchAgent 会在 30 秒内把它拉起来。
- 这个方案让你**从网页操作那台 Mac 上的 routine**，但 routine 本身仍是本地的，claude.ai/code/routines 页面不会列出它们。要让某个 routine 出现在那个页面并且电脑关机也照跑，得在 App 里用 **New routine → Cloud** 重建成云端 routine —— 云端跑在全新容器里，碰不到本机文件和 launchd，依赖本机脚本的 routine 需要先改造。

## 卸载后恢复

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --uninstall
# 如果也不想让手动会话自动上网页：
# 把 ~/.claude/settings.json 里的 "remoteControlAtStartup" 改成 false 或删掉
```

## 参考

- [Remote Control](https://code.claude.com/docs/en/remote-control)
- [Desktop scheduled tasks（本地 routine）](https://code.claude.com/docs/en/desktop-scheduled-tasks)
- [Routines（云端 routine）](https://code.claude.com/docs/en/routines)
- [跨会话消息](https://code.claude.com/docs/en/cross-session-messaging)
