---
description: 列出已装的 agent skill(直接输出 Markdown CATALOG,分类分段 + 完整描述)
---

**推荐做法(v8.1 新)**:直接输出 CATALOG.md 内容,而不是跑 `list` 命令。

原因:CATALOG.md 是 Markdown 格式(在 Claude Code / Codex 里原生渲染),尺寸精简过(<30KB 单次装得下)。ASCII 分类框虽好看但每条被截 80 字,用户看不到完整描述。

## 步骤

**1. 先确保 CATALOG.md 最新**
跑: `python {SKILLFORGE_PATH} catalog 2>&1`(强制重生成一次,反映最新状态)

**2. 读取并原样输出**
用 Read 工具读 `~/.skillforge/CATALOG.md`(Windows 路径 `C:/Users/Administrator/.skillforge/CATALOG.md`),把内容**原封不动 markdown 格式转给用户**。

CATALOG.md 结构:
- 头部:统计(74 普通 / 0 已定制 / 0 被遮蔽)
- 27 类分段(按成员数降序:🤖 Letta 9 / 🖼 Figma 8 / 🌊 GSAP 8 / ...)
- 每类内按 specificity+usage+name 三轴排
- 每 skill 显示:name + 版本快照(🟢/🟡/🔵)+ **完整描述**(无截断)

**3. 跑完提示**
- "想看某个详情用 `/skill详情 <编号或name>` 或 `/skill <编号>`"
- "想装新的去 GitHub 用 `/skill查找 <需求>`"
- "从已装推荐用 `/skill建议 <需求>`(不联网)"

## 兜底(CATALOG.md 恰好 >30KB 时)

如果读到的 CATALOG.md 超过 Bash tool / Read tool 上限,再退回分段跑:
- `python {SKILLFORGE_PATH} list --full 2>&1 | sed -n '1,280p'`
- `python {SKILLFORGE_PATH} list --full 2>&1 | sed -n '281,600p'`

分两条消息给用户看,一条都不能省略。

## 关键约束

**⚠️ 无论用哪种方式,必须让用户看到 74 个 skill 各自的完整功能描述,不能用"tight/brief 前 80 字"这种省略版应付**。用户是要判断"装哪个/改哪个/卸哪个",省略了就没法判断。
