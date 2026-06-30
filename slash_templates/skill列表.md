---
description: 列出已装的 agent skill,带编号
---

跑: `python {SKILLFORGE_PATH} list`

显示三段:
- 🟢 普通已装 (编号 1..N)
- 🟡 已定制(改过源码,标 ✨)
- ⚪ 被遮蔽副本(同名重复)

跑完告诉用户:
- "想看某个详情用 `/skill详情 <编号>` 或 `/skill <编号>`"
- "想装新的用 `/skill查找 <需求>`"
- "想改某个用 `/skill修改 <编号或name> <怎么改>`"
- "不要了用 `/skill卸载 <name>`"
