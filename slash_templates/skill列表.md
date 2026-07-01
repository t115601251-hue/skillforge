---
description: 列出已装的 agent skill,带编号
---

跑: `python {SKILLFORGE_PATH} list --full`

**⚠️ 必须用 `--full` 让每个 skill 描述完整展开(不能省略/截断)**。

显示**按 category 分段**(v8 新):
- 🤖 Letta agent / 🖼 Figma / 🌊 GSAP/动效 / 🚀 部署平台 / 📓 Notion / 🛡 安全 / 🎬 视频音频 / 🖌 图像处理 ...等 27 类
- 每段标计数(如 `━━━ 🖼 Figma  (8) ━━━`)
- 段内按 **specificity(专用度) + usage(使用频次) + 字典序** 排
- 末尾跟 🟡 已定制段(改过源码的)+ ⚪ 被遮蔽段(同名重复)

**⚠️ 输出通常 30KB+,超过 Bash tool 单次上限,一次跑完会被截**。**必须分两次跑**:
- 第一次: `python {SKILLFORGE_PATH} list --full 2>&1 | sed -n '1,280p'`
- 第二次: `python {SKILLFORGE_PATH} list --full 2>&1 | sed -n '281,600p'`

**两次输出各在一条消息里,绝对不能只出一次或用 --brief/tight 偷懒**。用户要看每个 skill 完整功能描述,不是"Use when the user..."这种被截尾的 80 字预览。

**⚠️ 重要:把 list 命令的完整输出原样转给用户,一行不少**。不要用 head/tail/head -N 截断,不要"...省略中间 N 个"。用户要看到所有编号才能选。如果列表特别长(>100 行),正常输出即可,scroll 是用户的事,不要替他截。

**附**:跑完会自动刷新 `~/.skillforge/CATALOG.md` 持久目录文档(完整描述 + 版本快照状态),也会同步 `~/.skillforge/.last_list.json` 编号缓存(供 `/skill <编号>` 引用)。

**显示模式可调**:
- `--full` 完整描述折行(适合细看单类)
- `--brief` 每条 120 字简介
- `--flat` 不分类,纯字母序(默认是分类分段)
- `--cat <模糊词>` 只看某类(如 `--cat 部署` 只看 🚀 部署平台)

跑完告诉用户:

跑完告诉用户:
- "想看某个详情用 `/skill详情 <编号>` 或 `/skill <编号>`"
- "想装新的用 `/skill查找 <需求>`"
- "想改某个用 `/skill修改 <编号或name> <怎么改>`"
- "不要了用 `/skill卸载 <name>`"
