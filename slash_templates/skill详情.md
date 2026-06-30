---
description: 看某个已装 skill 的详细信息
---

用户想看: $ARGUMENTS

跑: `python {SKILLFORGE_PATH} detail "$ARGUMENTS"`

**⚠️ 输出原样完整转给用户,不要截断/省略任何一行**(detail 输出本来就不长,用户要看完整 description 才能判断要不要改/卸载)。

它会显示:
- 当前位置 (skill_dir)
- 来源 GitHub URL
- 安装命令
- 定制状态(是否改过)
- 三槽位状态(pristine / previous / current 是否存在)
- 完整 description

把输出原样转给用户,然后顺便提示:
- "想改这个 skill: `/skill修改 $ARGUMENTS <怎么改>`"
- "想看简介: `/skill介绍 $ARGUMENTS`"
- "想卸载: `/skill卸载 $ARGUMENTS`"
