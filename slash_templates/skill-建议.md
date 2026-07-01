---
description: 自然语言路由 → 从已装 skill 里推荐 Top 3 (不去 GitHub,纯本地)
---

用户的需求: $ARGUMENTS

跑: `python {SKILLFORGE_PATH} suggest "$ARGUMENTS"`

它会:
- 用关键词加权匹配(实词重,泛词轻)
- 再融合 specificity(专用度)+ usage(使用频次)
- 出 Top 3 Markdown 表格,带"适合场景 / 不适合场景" 两列
- 真没命中(top base < 0.15)会直接说"本地没匹配"+提示去 GitHub

把表格**原样转给用户**(Markdown 表格在 Claude Code 渲染漂亮)。

跑完提示用户:
- 想看某个详情: `/skill-详情 <编号或name>` 或 `/skill <编号>`
- 都不合适去 GitHub 找: `/skill-查找 <需求>`
