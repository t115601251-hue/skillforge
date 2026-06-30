---
description: 看某个已装 skill 的详细信息(来源、安装命令、版本快照、定制历史)
---

用户想看的 skill: $ARGUMENTS

跑 `python {SKILLFORGE_PATH} detail "$ARGUMENTS"` 然后把内容原样输出给用户。

如果是数字编号($ARGUMENTS 看起来像数字),它会从上次 `/skill列表` 的编号缓存里查;
如果是名字,直接用。

输出后顺便提示:"想改这个 skill 用 `/skill修改 $ARGUMENTS <怎么改>`;想卸载用 `/skill卸载 $ARGUMENTS`。"
