---
description: 列出所有 /skill* 命令的用法
---

把下面这段原样转给用户:

```
/skill 命令体系 — agent 做 LLM 推理,skillforge 当工具

  /skill查找 <需求>           自然语言找 → Top 3 推荐(R/U/T 三维分 + Scorecard + OSV 安全审)
  /skill安装 <编号|owner/repo>  装一个,装完自动 intro
  /skill列表                  看已装(普通 / 已定制 / 被遮蔽 三段)+ 编号
  /skill <编号>               快捷:看第 n 个详情(等价 /skill详情)
  /skill详情 <编号|name>      看来源/安装命令/版本状态/定制历史
  /skill修改 <name> <需求>    agent 看源码,出 changes,自动快照,显 diff,确认应用
  /skill回滚 <name>            上一版 swap;--pristine 回 github 原版
  /skill卸载 <name>            删软链 + 搬 backups
  /skill介绍 <name>            agent 用自己的模型写一段简短中文说明
  /skill帮助                  本表

底层数据存储:
  ~/.skillforge/skills/<n>/       current (在用)
  ~/.skillforge/versions/<n>/     pristine + previous (三槽位)
  ~/.skillforge/backups/          各种备份
  ~/.skillforge/trusted.txt       自动允许 install 命令的 owner 白名单
  ~/.skillforge/.last_list.json   编号缓存 30 天

设计:agent (你) 是 LLM,skillforge 是工具。所有需要"理解 / 改写 / 判断"的步骤都由你做。
```

之后提示:"直接跟我说需求就行,会自动路由到对应命令。"
