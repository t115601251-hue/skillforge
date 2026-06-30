---
description: 列出已装的 agent skill,带编号
---

跑 `python {SKILLFORGE_PATH} list` 显示用户当前装的所有 skill。

输出有三段:
- 🟢 普通已装 (编号 1..N)
- 🟡 已定制(改过源码,标 ✨)
- ⚪ 被遮蔽副本(同名重复)

跑完后告诉用户:"想看某个详情用 `/skill <编号>`,要装新的用 `/skill查找 <需求>`,要改用 `/skill修改 <编号> <怎么改>`。"
