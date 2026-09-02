# 1234

工具脚本集合。

- [`mac/`](mac/README.md) — 把一台常开的 Mac 接到 claude.ai/code，并把这台机器上的内容和聊天记录弄到网页/手机上看得到：
  - `mac/setup-remote-control.sh` — 在 Mac 上常驻运行 Claude Code 的 Remote Control，让网页端和手机能看到、操作、并在这台 Mac 上新开会话；进程掉了自动拉起
  - `mac/claude-history.sh` — 只读地翻本机的聊天记录：列出 / 搜 / 看 / 导出 / 拉起旧对话；远程会话可以替你跑它
  - README 里有一张**对齐清单**：会话、聊天记录、routine、配置分别存在哪儿，桌面 App 和网页各自能看到什么，以及每一项怎么从网页端够到
