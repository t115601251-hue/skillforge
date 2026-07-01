---
description: 取消收藏一个已装 skill,catalog 里 ⭐ 前缀移除
---

用户想取消收藏: $ARGUMENTS

跑: `python {SKILLFORGE_PATH} unfavorite "$ARGUMENTS"`

- $ARGUMENTS 可以是编号或 name
- 本地操作,**不需要 GitHub token**
- 完成后 CATALOG 自动重生成,该项 ⭐ 前缀会消失

跑完把 CLI 输出原样转给用户。
