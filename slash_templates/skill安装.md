---
description: 装一个 skill(可以是编号、name 或 owner/repo)
---

用户想装: $ARGUMENTS

跑 `python {SKILLFORGE_PATH} install "$ARGUMENTS" --no-star`。

注意:
- 如果 $ARGUMENTS 是 owner/repo 格式 → 直接装
- 如果是数字 → 它会查 `/skill列表` 的缓存编号
- 如果是已装的 name → 它会提示"已经装过了"

安装命令(pip/npm install 等)**默认不跑**,除非 owner 在 ~/.skillforge/trusted.txt 白名单里。
如果想自动跑安装,提示用户 `/skill trust add <owner>`。

装完之后会自动 print intro(刚装的 skill 干什么、怎么触发),把这段原样转给用户。
