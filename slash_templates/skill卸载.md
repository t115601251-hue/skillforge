---
description: 卸载一个已装的 skill(数据搬到 backups/ 不丢)
---

用户要卸载: $ARGUMENTS

跑 `python {SKILLFORGE_PATH} uninstall "$ARGUMENTS"`。

它会:
1. 列出会删的 agent 软链(.claude/skills/<name> 等)
2. 把 skill 源目录 + versions/<name>/(pristine/previous)整体搬到 ~/.skillforge/backups/
3. 让用户 confirm 才执行(--yes 跳过)

误删可以从 backups/ 手动恢复。
