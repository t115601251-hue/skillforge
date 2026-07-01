---
description: 列出已装的 agent skill(MECE 5+1 分类,支持中英双语,读取 CATALOG.md)
---

**默认流程(v9,MECE 分类)**:输出 CATALOG.md 内容,按 MECE 5+1 分类(🟢 Data Fetcher / 🔵 Content Transformer / 🔥 Multi-Modal Generator / ⚡ Action Executor / 🛠 Integration Utility / 🚫 Native Infra 隔离)。

## 步骤

**1. 判断输出语言**
- 用户当前对话是中文 → 用 zh 生成
- 用户对话是英文 → 用 en 生成
- 混合时看最近一条消息的主语言

**2. 重生成对应语言的 CATALOG**
跑: `python {SKILLFORGE_PATH} catalog --lang zh 2>&1`(中文)
或:  `python {SKILLFORGE_PATH} catalog --lang en 2>&1`(英文)

**3. 读取并原样输出**
用 Read 工具读 `~/.skillforge/CATALOG.md`(Windows 路径 `C:/Users/Administrator/.skillforge/CATALOG.md`),把内容**原封不动 markdown 格式转给用户**。

CATALOG.md 结构(MECE):
- 头部统计(普通/已定制/被遮蔽 计数)
- 5 类业务 + 1 类隔离 = 6 段,固定顺序
  - 🟢 Data Fetcher / 数据感知与检索
  - 🔵 Content Transformer / 内容转化与处理
  - 🔥 Multi-Modal Generator / 多模态创作
  - ⚡ Action Executor / 动作执行与控制
  - 🛠 Integration Utility / 跨组件集成工具
  - 🚫 Native Infrastructure (isolated) / 系统原生基建(隔离)
- 每类头显示"数据契约 / 编排建议"元信息
- 每 skill 显示:name + 版本快照 emoji(🟢/🟡/🔵)+ **完整描述**

**4. 跑完提示用户**
- 想看某个详情:`/skill详情 <编号或name>` 或 `/skill <编号>`
- 想装新的(GitHub):`/skill查找 <需求>`
- 想从已装推荐(本地):`/skill建议 <需求>`

## 兜底(CATALOG.md 恰好 >30KB 时)

跑: `python {SKILLFORGE_PATH} list --full 2>&1 | sed -n '1,280p'` 加 `sed -n '281,600p'`,分两条消息给用户。

## 关键约束

**⚠️ 完整描述,不省略**。用户判断"装哪个/改哪个/卸哪个"必须看到全文,不能用 tight/brief 前 80 字应付。
