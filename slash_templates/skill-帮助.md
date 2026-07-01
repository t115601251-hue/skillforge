---
description: 列出所有 /skill-* 命令的用法
---

把下面这段原样转给用户:

```
/skill-* 命令体系 — agent 做 LLM 推理,skillforge 当工具
(v9.6: 加收藏功能;v9.3 命令名统一加 - 分隔,避免与内置斜杠命令冲突)

  /skill-查找 <需求>         自然语言找 → Top 3 推荐(R/U/T 三维分 + Scorecard + OSV)
  /skill-建议 <需求>         从已装里选 Top 3(不联网,给"适合/不适合"评价)
  /skill-列表                MECE 5+1 分类的紧凑目录(带编号,🏠=本工具,⭐=收藏)
  /skill-详情 <编号|name>    看来源/安装命令/版本状态/定制历史
  /skill-安装 <编号|owner/repo>  装一个,装完自动 intro
  /skill-修改 <name> <需求>  agent 看源码出 changes,自动快照,显 diff,确认应用
  /skill-回滚 <name>          上一版 swap;--pristine 回 github 原版
  /skill-卸载 <name>          删软链 + 搬 backups
  /skill-介绍 <name>          agent 用自己的模型写一段简短中文说明
  /skill-创建 <需求>          从零造一个新 skill (v9.7,参考 Anthropic skill-creator)
  /skill-收藏 <编号|name>     ⭐ 收藏 (v9.6,本地存,catalog 里前缀 ⭐)
  /skill-取消收藏 <编号|name> 去掉 ⭐
  /skill-帮助                本表

ASCII 别名(同一命令,自动补全下拉框搜得到):
  /skill-find /skill-suggest /skill-list /skill-info /skill-install
  /skill-modify /skill-rollback /skill-uninstall /skill-intro /skill-help
  /skill-favorite /skill-unfavorite /skill-create

图标含义(catalog 里):
  🏠  本工具自身 (skillforge)
  ⭐  你收藏的 skill
  ✨  已被 modify 定制过

底层数据存储:
  ~/.skillforge/skills/<n>/       current (在用)
  ~/.skillforge/versions/<n>/     pristine + previous (三槽位)
  ~/.skillforge/backups/          各种备份
  ~/.skillforge/trusted.txt       自动允许 install 命令的 owner 白名单
  ~/.skillforge/.last_list.json   编号缓存 30 天
  ~/.skillforge/.favorites.json   收藏 (v9.6)
  ~/.skillforge/CATALOG.md        紧凑目录,MECE 5+1 分类,每次装卸/改写自动重生成

设计:agent (你) 是 LLM,skillforge 是工具。所有需要"理解 / 改写 / 判断"的步骤都由你做。
```

之后提示:"直接跟我说需求就行,会自动路由到对应命令。"
