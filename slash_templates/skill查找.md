---
description: 自然语言找一个能解决用户需求的 agent skill
---

用户想找一个能干以下事情的 skill: $ARGUMENTS

请按以下步骤(每一步都告诉用户你在做什么):

1. 跑 `python {SKILLFORGE_PATH} which "$ARGUMENTS"` —— 看有没有已装的能命中
2. 如果命中,直接告诉用户用法,结束
3. 没命中就跑 `python {SKILLFORGE_PATH} find "$ARGUMENTS" --no-star` —— LLM 流水线找 Top 3
4. 把 Top 3 输出**原样**展示给用户(三维分 + 推荐理由 + 风险标签)
5. **不要替用户做选择**,问"装第几个?(给序号或 owner/repo,n 取消)"
6. 用户选完后跑 `/skill安装 <他给的>` 或直接 `python {SKILLFORGE_PATH} install <他给的> --no-star`

注意:看到 🔴 红色风险标签的候选,要在推荐前**口头复述风险**让用户清楚。
