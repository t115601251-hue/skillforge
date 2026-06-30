---
description: 装一个 skill(可以是编号、name 或 owner/repo)
---

用户想装: $ARGUMENTS

## 步骤

**1. 跑 install 命令**
跑: `python {SKILLFORGE_PATH} install "$ARGUMENTS" --no-star`

它会:
- $ARGUMENTS 是 owner/repo → 走 simple 路径直装(不调 LLM)
- $ARGUMENTS 是数字 → 查 `/skill列表` 编号缓存
- 已装的 name → 提示"已经装过了"

安装命令(pip/npm install 等)**默认不跑**,除非 owner 在 ~/.skillforge/trusted.txt 白名单里。

**2. 装完自动 intro**
install 末尾会自动调 `intro` 给个简短介绍。如果你想用更口语的版本:

跑 `/skill介绍 <刚装的 name>` 让你(agent)亲自写一段。

**3. 提示用户**
"装好了。说'用 <name> 帮我 ...'就能触发。
不喜欢可以 `/skill修改 <name> <需求>` 改;不要了 `/skill卸载 <name>`。"
