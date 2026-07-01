---
description: 列出已装的 agent skill (紧凑格式,英文名 + 一句中/英释义,MECE 5+1)
---

## 输出格式 (v9.1 用户认定,严格遵守)

**不要贴完整英文长描述**。每个 skill 只输出一行:**加粗英文名** + em-dash + 一句 ≤ 25 字/12 words 的释义。
按 MECE 5+1 分段,每段给一行数据契约/编排建议摘要。中文对话默认中文,英文对话默认英文。

## 步骤

**1. 判断输出语言**
- 用户当前对话是中文 → `--lang zh`
- 用户对话是英文 → `--lang en`
- 混合时看最近一条消息的主语言

**2. 重生成紧凑版 CATALOG**
```
python {SKILLFORGE_PATH} catalog --brief --lang zh   # 中文
python {SKILLFORGE_PATH} catalog --brief --lang en   # 英文
```
`catalog` 默认走 brief 模式(用户 v9.1 定的);要看完整长描述才加 `--full`。

**3. 读取并原样输出**
用 Read 工具读 `~/.skillforge/CATALOG.md`(Windows: `C:/Users/Administrator/.skillforge/CATALOG.md`),把内容**原封不动 markdown 格式转给用户**。产物已经是紧凑格式,不需要再压缩、再改写。

CATALOG.md 结构:
- 头部统计(普通/已定制/被遮蔽 计数)
- 5 类业务 + 1 类隔离 = 6 段,固定顺序
  - 🟢 Data Fetcher / 数据感知与检索
  - 🔵 Content Transformer / 内容转化与处理
  - 🔥 Multi-Modal Generator / 多模态创作
  - ⚡ Action Executor / 动作执行与控制
  - 🛠 Integration Utility / 跨组件集成工具
  - 🚫 Native Infrastructure (isolated) / 系统原生基建(隔离)
- 每类头显示"数据契约 / 编排建议"元信息
- 每 skill 一行:`- **name** <sub>版本 emoji</sub> — 一句释义`

**4. 尾部提示**
- 想深挖某个:`/skill-详情 <name>`
- 想装新的(GitHub):`/skill-查找 <需求>`
- 想从已装推荐(本地):`/skill-建议 <需求>`

## 兜底

- 用户点名要看某个 skill 的完整原描述 → `python {SKILLFORGE_PATH} catalog --full --lang zh` 再 Read
- 想只看某分类 → `python {SKILLFORGE_PATH} list --cat 数据 --lang zh`
- catalog 因为极端场景 >30KB → 分段 `python {SKILLFORGE_PATH} list --full --lang zh | sed -n '1,280p'` 与 `sed -n '281,600p'`

## 关键约束

- **不要自作主张改写释义**:brief 模式产物是用户认可的措辞,直接搬。
- **不要略过任何一项**:74 就写 74 行,一个都不省。
- **不要贴长英文原描述**:除非用户点名 `--full`。
