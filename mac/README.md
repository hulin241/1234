# 把常开的 Mac 接到 claude.ai/code

## 问题

网页端 claude.ai/code 只显示**云端**的东西。桌面 App 里的会话、本地 routine、还有你过去的聊天记录，都在那台 Mac 上，网页端从设计上就看不到它们 —— 不是同步坏了。

官方文档说得很直白：[sessions](https://code.claude.com/docs/en/sessions) —— “The desktop app, Claude Code on the web, and the VS Code extension each maintain their own session history. This page covers the CLI.”；[desktop](https://code.claude.com/docs/en/desktop#coming-from-the-cli) 又补了一句 CLI 和桌面 App 的关系 —— “Each maintains separate session history, but they share configuration and project memory via CLAUDE.md files.” 跨设备天然能看到的只有两类：跑在 Anthropic 云上的会话，和你主动开了 **Remote Control** 的那个本地会话。

这个目录里有两个脚本：

- **`setup-remote-control.sh`** —— 让这台 Mac 常驻在线：网页端和手机能操作它，也能在它上面新开会话，进程掉了 30 秒内自动拉起
- **`claude-history.sh`** —— 在这台 Mac 上把本地聊天记录列出来 / 搜 / 导出 / 拉起。它是只读的，远程会话可以替你跑它，于是你在网页端也能翻本机的记录

## 对齐清单：什么在哪儿看得到

| 东西 | 存在哪里 | 桌面 App | 网页 / 手机 | 怎么从网页端够到 |
|---|---|---|---|---|
| 此刻正在跑的会话 | 你的 Mac | 只列 App 自己开的（不含终端 CLI 的） | 只列开了 Remote Control 的 | 装本目录的常驻服务 |
| 过去的对话（聊天记录） | `~/.claude/projects/<项目>/<会话ID>.jsonl` | 侧边栏只列 App 自己的 | ❌ 不列 | 让远程会话跑 `claude-history.sh` |
| 你打过的每一句 prompt | `~/.claude/history.jsonl` | CLI 里 Ctrl+R | ❌ | `claude-history.sh prompts` |
| 云端会话 | Anthropic 云 | ✅ 侧边栏（Cloud） | ✅ 天生可见 | 本来就通 |
| 本地 routine | prompt 在 `~/.claude/scheduled-tasks/<名字>/SKILL.md`；时间 / 目录 / 模型 / 暂停状态不在文件里 | ✅ Routines 页 | ❌ 不列 | 远程会话可以读、改 SKILL.md；其余只能在 App 里改 |
| 云端 routine | claude.ai 账号 | ✅ 同一个 Routines 页 | ✅ claude.ai/code/routines | 本来就通 |
| 配置（CLAUDE.md / settings.json / MCP / skills / hooks） | `~/.claude` + 项目目录 | ✅ 和 CLI 共用 | 云端会话只吃**提交进仓库**的那份 | 想让云端行为一致，就把配置提交进仓库 |
| 本机文件 | Mac 硬盘 | ✅ | 云端会话是一份全新克隆，看不到本机文件 | Remote Control（会话跑在你的 Mac 上） |
| 侧聊（Cmd+;、`/btw`） | 不落盘 | 关掉就没了 | ❌ | 无 |
| 从手机派活给这台 Mac | Dispatch（Cowork 标签） | ✅ 会话带 Dispatch 徽章 | 手机 App 里发任务 | 手机和桌面 App 配对一次，不用装脚本（Pro / Max，Team、Enterprise 没有） |

两点说明：

- 表里"❌"的那几行不是设置问题，是产品边界。能改变的只有"怎么够到"那一列。
- `~/.claude/projects/` 是官方为 Claude Code 记录 transcript 的位置（[sessions](https://code.claude.com/docs/en/sessions)）。桌面 App / Cowork 的记录**很可能也在这里**（官方的清理规则专门为它们开了例外，见下面"保留多久"），但文档没有直说路径。想看你这台机器上的实际情况，跑 `claude-history.sh list`：第四列是每份记录里的 `entrypoint` 字段（`cli` / `desktop` / `remote` …），能大致看出是谁开的 —— 这个字段官方没公开过，版本之间可能变，只能当参考。

## 两种做法

### 做法 0：不用脚本（最简单）

在常开的 Mac 上打开 Claude 桌面 App：**Settings → Claude Code → Enable remote control by default**，然后在 Code 标签里**保持一个会话开着**。这个会话会出现在另一台电脑的 claude.ai/code 侧边栏和手机 App 里，你可以直接操作它。

限制：只有你在 App 里开着的会话才可见；App 退出、Mac 重启后要重新打开。

### 做法 1：本脚本（常驻、可从手机随时新开会话）

在 Mac 上跑一个常驻的 `claude remote-control` 服务（server mode）。会话仍然在这台 Mac 上执行（文件、本地脚本全都可用）；进程意外退出 30 秒内自动拉起；用户登录后自动启动。server mode 默认最多同时服务 32 个会话，手机上从设备卡片新开的会话就是它创建的。

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
git clone -b claude/content-app-version-sync-5yzxxa https://github.com/hulin241/1234.git ~/claude-remote-control
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

- 另一台电脑打开 https://claude.ai/code ，侧边栏里出现这台 Mac 的会话（带电脑图标，在线时是绿点）
- 手机 Claude App 里同样能看到；**Code 标签顶部还会出现这台机器的卡片**，点进去选一个目录就能在这台 Mac 上开一个新会话（出处是 [What's new · Week 34](https://code.claude.com/docs/en/whats-new/2026-w34)，Remote Control 参考页没写这张卡片；文档只写了手机 App 这么用，网页端有没有以你看到的为准）
- 你在这台 Mac 上手动开的 `claude` 会话，也会自动出现在网页端（`remoteControlAtStartup`）
- 在网页/手机上改会话名字，会写回本机 —— `claude --resume` 里看到的标题跟着变（需要 claude ≥ 2.1.221）
- 在这个远程会话里可以用大白话支使**这台机器上的其它会话**：“看看现在还有哪些会话在跑”“跟跑迁移那个会话说一声 schema 改了” —— 靠的是跨会话消息（claude ≥ 2.1.224）。注意传过去的只有你这句话，**不含对方的对话历史**
- 网页/手机上能用的斜杠命令是有限的一批（`/compact`、`/clear`、`/context`、`/model`、`/effort`、`/rename`、`/recap`…）。**`/resume` 只能在本机终端用**（官方原话），`/export` 也不在官方列出的"手机 / 网页可用"名单里，所以同样得在本机跑，所以翻旧对话要靠下面那节的脚本
- 关于本地 routine：远程会话能读 `~/.claude/scheduled-tasks/<名字>/SKILL.md` 看每个 routine 的 prompt，也能直接改（下次运行生效）。**暂停、改时间、改目录、改模型仍要在桌面 App 的 Routines 页面做** —— 这些不在文件里

## 聊天记录（history）

### 存在哪、留多久

| 文件 | 内容 | 保留多久 |
|---|---|---|
| `~/.claude/projects/<项目>/<会话ID>.jsonl` | 完整对话：每条消息、每次工具调用和结果 | 默认 **30 天**，`cleanupPeriodDays` 可改。桌面 App / Cowork 里开的会话默认**不按天数删** —— 但这要 claude ≥ 2.1.248，更早的版本照样按 `cleanupPeriodDays` 删；想给它们一个上限就设 `desktopSessionCleanupPeriodDays`，公司用 managed settings 设了 `cleanupPeriodDays` 时也按那个值删 |
| `~/.claude/history.jsonl` | 你打过的每一句 prompt，带时间和项目路径 | **不自动清理**，删了才没 |
| `~/.claude/projects/<项目>/<会话ID>/subagents/` | 子 agent 的对话 | 跟着主 transcript 一起删 |
| `~/.claude/projects/<项目>/<会话ID>/tool-results/` | 被单独存出去的大段工具输出 | 同样按 `cleanupPeriodDays` 到期删 |
| `~/.claude/uploads/<会话>/` | 你从手机 / 网页发进 Remote Control 会话的附件 | 同样按 `cleanupPeriodDays` 到期删 |

两个坑：这些文件是**明文**，工具读过的 `.env`、命令打印出来的密钥都会原样躺在里面；另外文件名里带 `.orphaned-`、或者以 `.superseded-<时间戳>` 结尾的，是被搁置的旧版本记录，`/resume` 和本脚本都不列它们。

### 从这台 Mac 上翻记录

```bash
H=~/claude-remote-control/mac/claude-history.sh

bash $H list                      # 所有会话，按最后活动倒序；● 表示此刻正开着
bash $H list --dir ~/code/x       # 只看某个项目
bash $H show <会话ID前几位>        # 打印整段对话（Markdown）
bash $H show last --limit 20      # 只看最后 20 条 —— 在手机上翻旧对话就用这个
bash $H show last --tools         # 最近那个会话，连工具调用一起看
bash $H search "登录 bug"          # 全文搜所有会话
bash $H prompts 部署               # 只搜你自己打过的 prompt（再拿它去 search 定位是哪段对话）
bash $H export a1b2c3d4 --out ~/chat.md
bash $H export all --out ~/chat-backup    # 全部导出，一份会话一个 .md
bash $H routines                  # 桌面 App 的本地 routine：名字 + prompt + 文件路径
bash $H where                     # 记录在哪、占多大、保留多少天
bash $H live                      # 此刻正在跑的会话（含 App / Remote Control 开的）
```

只读你的聊天记录：不动 `~/.claude/projects` 下的任何一份 transcript。唯一会写文件的是 `export` —— 给了 `--out` 就写那个路径，没给就在当前目录写 `claude-<短ID>.md`。（`list` / `show` / `resume` / `live` 会调一次 `claude agents --json`，用来标出此刻正开着的会话。）

默认还会把 `<system-reminder>` 这类工具注入的内容藏掉，想看原样加 `--raw`；工具调用和 thinking 也默认不显示，用 `--tools` / `--thinking` 打开。

### 从网页端翻记录

在网页端那个远程会话里，直接让它跑就行，例如：

> 跑一下 `bash ~/claude-remote-control/mac/claude-history.sh search "上周那个部署脚本"`，把相关的会话 ID 告诉我

找到之后再让它 `show <会话ID> --limit 20` 看结尾那几句。想看本地 routine 就让它跑 `claude-history.sh routines`。

输出会回到网页/手机上。注意 `export` 写出来的 `.md` 是落在 **Mac 上**的，手机/网页那边下载不到 —— 要么让远程会话直接把内容念出来，要么自己 scp / AirDrop / 提交进一个仓库。这是目前把**过去**的本机对话弄到网页端看的办法 —— Remote Control 本身只同步它连着的那一个会话的内容，`/resume` 在网页和手机上不能用；就算你在本机 `/resume` 切到另一段对话，连着的设备也拿不到那段对话的历史。

### 官方自带的几招

- `/export [文件名]`：在本机终端里把当前对话导成可读文本。官方只点名 `/plugin`、`/resume` 是"仅本机"，`/export` 不在"手机 / 网页可用"的名单里，所以是推断
- Ctrl+R：搜你打过的 prompt（就是 `history.jsonl`）；Ctrl+O：看当前会话的 transcript，全屏下 `/` 还能搜
- `/resume` 的选择器默认只显示当前 worktree 的会话，**Ctrl+A 才会列出这台机器上所有项目的** —— 很多"我的旧会话不见了"其实是这个
- `claude -p --resume <会话ID> --output-format json`：不进交互界面读一段旧会话
- `claude project purge <路径>`：删掉某个项目的全部本地状态 —— transcript、**auto memory**（Claude 给自己记的项目笔记）、tasks / debug / file-history、`history.jsonl` 里对应的行、以及 `~/.claude.json` 里这个项目的条目。会先打印一份删除清单让你确认

> `.jsonl` 每行的结构是**内部实现，版本之间会变**，官方明确不建议直接解析。`claude-history.sh` 就是在解析它，所以 claude 大版本更新后如果它显示不对，先用上面这些官方命令顶一下，再来修脚本。

## "拉起"：五种

0. **从手机直接派活（最省事，什么都不用装）** —— 手机 Claude App → Cowork 标签 → Dispatch，把任务说给它。它判断是开发活儿就在这台 Mac 的桌面 App 里开一个带 **Dispatch** 徽章的 Code 会话，干完或者需要你批准时推送通知。前提是手机和桌面 App 配对过、App 开着；Pro / Max 才有，Team / Enterprise 没有。
1. **服务掉了自己起来** —— LaunchAgent 的 `KeepAlive`：进程退出 30 秒内重启。网络断超过约 10 分钟，`claude remote-control` 会自行退出，靠的就是它拉回来。
2. **从手机在这台 Mac 上开个新会话** —— Claude App → Code 标签顶部的机器卡片 → 选目录 → 新会话。想让这些会话各自待在自己的 git worktree 里、不互相踩，装的时候加 `--spawn worktree`。
3. **把停掉的 Remote Control 会话拉回来（只适用于手动跑 server 的情况）** —— 装了本目录这个常驻服务的话，进程一退 `KeepAlive` 30 秒内又拉起来了，没有"停掉"这回事；真要手动接管，先 `--uninstall` 停掉常驻服务。手动跑的 server 被 Ctrl+C 之后**约 4 小时内**，在同一个目录里：`claude remote-control`（全部拉回）、`claude remote-control --continue`（只拉它最初那个）、`claude remote-control --session-id <ID>`（ID 是 claude.ai/code 链接里 `/code/` 和 `?` 之间那段）。前提是这期间没在同一个目录里另起一个 `claude remote-control`。
4. **把一段旧对话接着聊** ——
   ```bash
   bash $H resume <会话ID前几位>     # 打印下面这几条命令，填好目录和完整 ID
   cd <那个目录> && claude --resume <会话ID>            # 在 Mac 的终端里接着聊
   cd <那个目录> && claude --bg --resume <会话ID> "继续" # 从远程会话里后台拉起，再用 claude logs <短id> 看输出
   ```
   想让这段旧对话**出现在网页端**，有三条路：
   - 终端里：先 `claude --resume` 打开它，再执行 `/remote-control` —— 它会带着当前这段对话的历史一起过去
   - 如果这段对话以前开过 Remote Control：`claude --resume` 会照它的 reconnection record 自动接回原来那个 claude.ai 会话，不用再做什么
   - 如果它是桌面 App 里的会话：会话工具栏右下角 **Continue in → Claude Code on the Web** —— 推分支、带上对话摘要，在**云端**另开一个会话（要求工作区干净，SSH 会话不行）。之后 Mac 关机也照跑，网页天生列得到

   注意别同时在两个终端 resume 同一个会话：两边的消息会交织进同一份 transcript。

反方向（云端会话拉到本机）用 `claude --teleport <会话ID>`，要求工作区干净、仓库对得上、分支已推、同一个账号。

## 常用命令

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --status      # 状态 + 最近日志（已去掉控制字符）
bash ~/claude-remote-control/mac/setup-remote-control.sh --uninstall   # 卸载常驻服务
bash ~/claude-remote-control/mac/claude-history.sh where               # 聊天记录概况
```

日志在 `~/Library/Logs/claude-remote-control.log`，每次重新安装会把旧的挪到 `.log.1`。

## 可选参数

`setup-remote-control.sh`：

| 参数 | 作用 |
|---|---|
| `--dir <路径>` | 指定工作目录（默认当前目录） |
| `--name <名字>` | 网页端看到的会话名（默认主机名） |
| `--permission-mode <模式>` | 会话权限模式：default / acceptEdits / plan / auto / dontAsk / bypassPermissions / manual；默认不传 |
| `--spawn <模式>` | 从设备新开的会话怎么放：`same-dir`（claude 的默认）/ `worktree`（各自一个 git worktree，要求工作目录是 git 仓库）/ `session`（只服务一个会话） |
| `--capacity <N>` | 最多同时服务几个会话（claude 的默认值是 32；和 `--spawn session` 互斥） |
| `--skip-trust-check` | 跳过目录信任 / 首次确认检测 |

`--spawn` 和 `--capacity` 会先在这台机器的 `claude remote-control --help` 里确认存在才写进服务 —— 旧版本不认识的 flag 会让后台服务起不来并被反复重启。

`claude-history.sh`：`--dir` `--limit` `--out` `--tools` `--thinking` `--raw` `--json`，`bash claude-history.sh --help` 有完整说明。`--limit` 对 `list` / `search` / `prompts` 是"显示几条"，对 `show` / `export` 是"只看最后几条"。

## 注意

- **是"登录后自启"，不是"开机自启"**。LaunchAgent 装在登录会话里，Mac 重启后要有人登录一次才会启动。要做到真正无人值守：系统设置 → 用户与群组 → 自动登录 选这个用户（开了 FileVault 时没有这个选项），并把 Claude App 加入登录项。
- **Mac 必须保持唤醒并联网**。休眠期间网页端看到的是"离线"。系统设置 → 电池/节能 里关掉自动休眠；或 Claude App 设置 → Desktop app → General → **Keep computer awake**（只在桌面 App 运行期间有效）。合上盖子仍会休眠。
- **项目别放在 桌面/文稿/下载/iCloud/外接盘**。这些目录受 macOS 隐私保护，后台服务第一次访问会在屏幕上弹授权框，没人点就卡住。放到 `~/code` 之类的目录最省事；或在 系统设置 → 隐私与安全性 → 完全磁盘访问权限 里加入 claude。
- **登录钥匙串不要设自动锁定**。后台服务读取登录凭据时会弹解锁框。脚本会检测并提示。
- **claude 的登录会过期**。过期后远程会话停止响应，网页端看不出原因。在这台 Mac 上重新 `claude auth login`，然后重跑一次脚本。`--status` 会在日志里发现相关提示时提醒你。
- **连着 Remote Control 的时候，这段对话的 transcript 也存在 Anthropic 服务器上** —— 跨设备同步靠的就是它。没连的时候，记录只在本机。
- **本地 routine 只在桌面 App 运行、且 Mac 未休眠时执行**。这个方案让你从网页操作那台 Mac，但 routine 本身仍是本地的，claude.ai/code/routines 页面不会列出它们。要让某个 routine 出现在那个页面并且电脑关机也照跑，得在 App 里用 **New routine → Cloud** 重建成云端 routine —— 云端跑在全新容器里，碰不到本机文件，依赖本机脚本的 routine 需要先改造。

## 卸载

```bash
bash ~/claude-remote-control/mac/setup-remote-control.sh --uninstall
# 如果也不想让手动会话自动上网页：
# 把 ~/.claude/settings.json 里的 "remoteControlAtStartup" 改成 false 或删掉
```

`claude-history.sh` 不需要卸载，它只是个只读脚本。

## 参考

- [Remote Control](https://code.claude.com/docs/en/remote-control)
- [会话：resume / 转录存在哪](https://code.claude.com/docs/en/sessions)
- [.claude 目录里都有什么](https://code.claude.com/docs/en/claude-directory)（transcript 路径、保留规则）
- [数据用途与保留](https://code.claude.com/docs/en/data-usage)
- [桌面 App](https://code.claude.com/docs/en/desktop)
- [Desktop scheduled tasks（本地 routine）](https://code.claude.com/docs/en/desktop-scheduled-tasks)
- [Routines（云端 routine）](https://code.claude.com/docs/en/routines)
- [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web)
- [跨会话消息](https://code.claude.com/docs/en/cross-session-messaging)
