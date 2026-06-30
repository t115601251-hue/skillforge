---
description: 回滚已修改的 skill 到上一版或 GitHub 原版
---

用户要回滚: $ARGUMENTS

解析 $ARGUMENTS:
- 默认 `<name>` → swap 模式:current ↔ previous 互换(再回滚一次回到原状)
- `<name> --pristine` → 强制回 GitHub 原版,当前 current 保存为 previous

跑 `python {SKILLFORGE_PATH} rollback $ARGUMENTS`。

如果 $ARGUMENTS 只有名字没说要不要 --pristine,优先 swap(更轻量)。如果用户明确说"回到原始版/原版/github 那版",再加 --pristine。
