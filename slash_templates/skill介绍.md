---
description: 给某个已装 skill 出一段简短的中文使用说明 (agent 自己写)
---

用户要看的 skill: $ARGUMENTS

**重要**: 不调 skillforge intro 命令(那个会调 Anthropic API),由你直接写。

## 步骤

**1. 拿原始信息**
跑两个命令:
- `python {SKILLFORGE_PATH} detail "$ARGUMENTS" 2>&1` —— 拿元信息(来源/安装命令/版本快照)
- `cat ~/.skillforge/skills/$ARGUMENTS/SKILL.md`(如果是编号,先 detail 拿到 name)

**2. 你写一段口语化中文介绍**(80-150 字)
包含三点:
- **它能帮你做什么** (一句话)
- **怎么触发它** (跟 agent 说什么样的话,从 SKILL.md description 的"触发"关键词抠)
- **一个最常用的调用例子**

格式建议:
```
✨ <name>
做什么: ...
怎么触发: 跟 agent 说"..."
例子: 跟 agent 说"用 <name> 帮我 ..."
```

**3. 展示给用户**

不需要再调任何 CLI。
