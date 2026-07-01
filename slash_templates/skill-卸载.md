---
description: 卸载一个已装的 skill (数据搬 backups/ 不丢)
---

用户要卸载: $ARGUMENTS

跑: `python {SKILLFORGE_PATH} uninstall "$ARGUMENTS"`

它会:
1. 列出会删的 agent 软链(.claude/skills/<name> 等)
2. 把 skill 源目录 + versions/<name>/(pristine+previous)整体搬到 ~/.skillforge/backups/
3. 让用户 confirm 才执行(--yes 跳过)

误删了可以从 backups/ 手动恢复(还原 skill_dir 后 register_skill 重新软链)。

提示用户卸载后:"该 skill 已下线。如果想重装直接 `/skill-查找 <需求>` 重找,或精确 `/skill-安装 owner/repo`。"
