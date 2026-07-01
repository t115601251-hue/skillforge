---
description: 收藏一个已装 skill,在 catalog 里前缀 ⭐
---

用户想收藏: $ARGUMENTS

跑: `python {SKILLFORGE_PATH} favorite "$ARGUMENTS"`

- $ARGUMENTS 可以是编号(先从 `~/.skillforge/.last_list.json` 缓存拿 name)或 name
- 收藏写到 `~/.skillforge/.favorites.json`,catalog 重生成时会在该项前加 ⭐
- 本地操作,**不需要 GitHub token**

跑完把 CLI 输出原样转给用户,顺便提示:"想看当前所有收藏:`/skill-列表` 里带 ⭐ 的就是;取消:`/skill-取消收藏 $ARGUMENTS`"
