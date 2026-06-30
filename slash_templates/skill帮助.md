---
description: 列出所有 /skill* 命令的用法
---

请输出下面这张表给用户(原样,不要重新组织):

```
/skill命令体系 — 自然语言驱动的 agent skill 管理

/skill查找 <需求>          自然语言找,Top 3 推荐(三维分 + 安全审查)
/skill安装 <编号|owner/repo>  装一个,自动 intro
/skill列表                 看已装,三段(普通 / 已定制 / 被遮蔽)+ 编号
/skill <编号>              快捷:看第 n 个详情(等价于 /skill详情 n)
/skill详情 <编号|name>     看来源、安装命令、版本状态、定制历史
/skill修改 <编号|name> <需求>   LLM 改源码(自动快照 → diff → 应用)
/skill回滚 <name> [--pristine]   回上一版(swap)或回 GitHub 原版
/skill卸载 <name>          删软链 + 搬 backups
/skill介绍 <name>          一段口语中文使用说明
/skill帮助                 本表

底层 CLI:  python {SKILLFORGE_PATH} <subcommand>
本地数据:  ~/.skillforge/  (skills/ + versions/ + backups/ + trusted.txt + .last_list.json)
```

提示用户:"通常你直接说需求就行,我会路由到对应的 /skill 命令。"
