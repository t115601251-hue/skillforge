---
description: 通用入口,自动路由到具体 /skill* 子命令 (容错带空格写法 /skill 帮助 等)
---

用户的输入: $ARGUMENTS

## 任务

按 $ARGUMENTS 的**第一个 token** 路由到对应的 /skill* 命令,**剩余部分作为参数**传过去。

## 路由表

| 第一个 token (可中英) | 路由到 |
|---|---|
| `帮助` / `help` / `h` / `?` | 执行 /skill帮助 流程 |
| `列表` / `list` / `ls` / `l` | 执行 /skill列表 流程 |
| `查找` / `find` / `search` / `q` | 执行 /skill查找 流程,传剩余作为需求(去 GitHub 找新的) |
| `建议` / `推荐` / `suggest` / `route` | 执行 /skill建议 流程,从**已装**里推荐 Top 3(不去 GitHub) |
| `安装` / `install` / `add` / `i` | 执行 /skill安装 流程,传剩余作为 target |
| `详情` / `detail` / `info` / `show` | 执行 /skill详情 流程,传剩余作为 target |
| `修改` / `modify` / `edit` / `custom` | 执行 /skill修改 流程,传剩余作为 "name + 需求" |
| `回滚` / `rollback` / `revert` / `undo` | 执行 /skill回滚 流程,传剩余作为 target |
| `卸载` / `uninstall` / `remove` / `delete` / `rm` | 执行 /skill卸载 流程,传剩余作为 target |
| `介绍` / `intro` / `describe` / `about` | 执行 /skill介绍 流程,传剩余作为 target |
| **纯数字** (如 `3`, `12`) | 等价 /skill详情 <数字>,直接看那个编号的详情 |

## 兜底

如果第一个 token **不在表里也不是数字**:
- 当作模糊的"查找"意图处理,**告诉用户**"我猜你想找一个能 X 的 skill,如果对就回 y,我接着 /skill查找;不对就 /skill帮助 看命令清单"
- 等用户确认再走 /skill查找

## 例子

- `/skill 帮助` → 路由到 /skill帮助
- `/skill 列表` → 路由到 /skill列表
- `/skill 查找 写作文的工具` → /skill查找 "写作文的工具"
- `/skill 3` → /skill详情 3
- `/skill 修改 asset-forge 让它默认输出 png` → /skill修改 "asset-forge 让它默认输出 png"
- `/skill 卸载 rembg` → /skill卸载 rembg
- `/skill 找一个能写作文的` → 第一个 token "找一个能写作文的" 不在表里,兜底走"猜你想 /skill查找"

## 注意

- 路由完之后,**直接执行对应命令的完整流程**(读对应模板的指引),不要再问用户一遍
- 中文 token 优先,容错英文别名
