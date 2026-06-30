# Spec: skillforge `find` 搜索 + 选用质量升级

- **Date:** 2026-06-30
- **Status:** Draft, awaiting user review
- **Owner:** skillforge
- **Touches:** `skillforge.py` (主要 `cmd_find` + 新增模块)、`skillforge_README.md`

---

## 1. 背景

当前 `skillforge find <query>` 流程:
1. 用户输入 query(中英文随意)
2. `github_search` 单次调用,按 stars 排序,取 top 5
3. 用户用序号挑一个(或 `--yes` 选 [0])

**已知问题:**
- **召回差**:用户预期日常输入是**中文口语化**(如"批量去图片背景"),GitHub 索引基本不含中文,单次直译关键词匹配偏。
- **stars 噪音大**:awesome 列表、过气热门库容易霸榜真正能用的工具。stars 是"一次性点赞",不反映"现在还在用"。
- **没有安全/治理信号**:archived/单一维护者/无 LICENSE/star farming 仓库混在结果里看不出来。
- **没有真实使用证据**:看不到下载量、watchers(持续订阅)、forks(被复制改用)、issue 闭合率(维护者有回应)。
- **选择是黑盒**:用户不知道为什么是这个排序、风险是什么、推荐理由是什么。

## 2. 目标

让 `find` 输入中文口语化需求,输出**带证据 + 带风险标记**的 Top 3 推荐,人选或 `--yes` 自动装。

**具体可衡量:**
1. 中文输入"批量去图片背景"能拿出 `rembg`、`asset-forge` 一类真匹配的候选,而不是 awesome-image-processing。
2. 任一候选都附 R(相关性)、U(使用证据)、T(治理透明)三维分,以及具体风险标签。
3. archived 仓库永远不是推荐 [0]。
4. 用户能看到"为什么推这个"的中文理由。
5. 整个 `find` 在有 `ANTHROPIC_API_KEY` + `GITHUB_TOKEN` 时,< $0.025 + < 12s。

## 3. 非目标

- **不**做 embedding / 向量库(成本/复杂度跟收益不匹配)
- **不**尝试拿别人仓库的 traffic/views(GitHub 不允许)
- **不**做包依赖审计(`requirements.txt` 里有没有恶意包) — v2 再说
- **不**改 `which` 的本地匹配逻辑(已经够用)
- **不**做 awesome 列表 grep(收益边际,延迟成本高,放进 backlog)
- **不**做交互式深度对话(本次只升级单次 `find`)

## 4. 总体设计

### 4.1 流水线 9 步

| 步 | 动作 | 调用 | 备注 |
|---|---|---|---|
| ① | LLM 改写 query | Anthropic ~300 tokens | 中文 → 3 个英文 query(功能/工具/技术栈三视角) |
| ② | `github_search` × 3 | GitHub × 3 | 合并去重得 10-15 候选 |
| ③ | 元数据体检 × N | GitHub × N | `/repos/{full_name}`;算出 T 分 + 风险 flags |
| ④ | LLM 粗排 | Anthropic ~1800 tokens | 从 N 个里挑 Top 5 继续深读 |
| ⑤ | 深读 Top 5 | GitHub × 10 | README + issue close 查询 |
| ⑥ | 包名推导 + 下载量 × Top 5 | GitHub raw × 5-10 + 外部 × 0-5 | raw 取 setup.py/package.json/Cargo.toml(无 API 限速);下载量公开免认证 |
| ⑦ | 重算 U 分 | 纯本地 | log10 加权融合所有使用证据 |
| ⑧ | LLM 终排 | Anthropic ~6600 tokens | 出 Top 3 + 推荐级别 + 中文理由 + 风险列表 |
| ⑨ | 渲染输出 | 纯本地 | 三维分 + emoji 信号 + 风险标黄 |

### 4.2 评分模型

**R 相关性(0-10)**:LLM 在第 ⑧ 步基于 (用户原始中文 query) + (候选 README 头 4000 字符) 直接打。**只有 LLM 能算**,启发式无能为力。

**U 使用度(0-100)**:启发式 log10 加权;权重见 §5.2。

**T 治理透明度(0-100)**:启发式;加分项 + 减分项见 §5.1。

**LLM 终排时同时拿到三维分,自己综合判断,不要让它再算一次分。** 它只负责定推荐级别(强推/推荐/谨慎/不推荐)和写中文理由。

### 4.3 推荐级别映射(参考,LLM 可微调)

| 级别 | 大致条件 |
|---|---|
| 强推 ⭐⭐⭐ | R ≥ 8, U ≥ 70, T ≥ 70, 0 红 flag |
| 推荐 ⭐⭐ | R ≥ 7, U ≥ 30, T ≥ 50, ≤ 2 黄 flag |
| 谨慎 ⭐ | R ≥ 6, 但 U < 30 或 T < 50 或有多个黄 flag |
| 不推荐 | R < 6 或有 🔴 archived |

`不推荐` 仍然展示在 Top 3 里(按"标红不过滤"策略),但 LLM 应该建议用户换个 query 或单独审一遍代码。

## 5. 评分细则

### 5.1 T(治理透明度)0-100

**加分(出现一项加分值)**
| 信号 | 来源 | 加 |
|---|---|---|
| `license` 非 null | metadata | +20 |
| `default_branch` ∈ {main, master, develop} | metadata | +15 |
| `pushed_at` 在 90 天内 | metadata | +15 |
| `contributors_count` ≥ 3 | metadata | +10 |
| 仓库年龄 ≥ 90 天 | metadata | +10 |
| `has_issues == true` | metadata | +10 |
| owner.type == "Organization" | metadata | +5 |
| `topics` 非空 | metadata | +5 |

**减分(出现一项减分值)**
| 信号 | 减 |
|---|---|
| `archived == true` | -100(实质淘汰到 0) |
| 仓库年龄 < 14 天 且 stars > 100 | -30("star farming 嫌疑") |
| `contributors_count == 1` 且 stars > 100 且年龄 < 60 天 | -20 |
| `pushed_at` 距今 > 365 天 | -10 |

总分 clamp 到 [0, 100]。

### 5.2 U(使用度)0-100

`U = clamp( w_s + w_w + w_f + w_d + w_r + w_c , 0, 100 )`

| 子项 | 公式 | 上限 |
|---|---|---|
| `w_s` 星标 | `log10(stars+1) / 5 * 20` | 20(stars ≥ 100k) |
| `w_w` 订阅 | `log10(watchers+1) / 4 * 20` | 20(watchers ≥ 10k) |
| `w_f` forks | `log10(forks+1) / 4 * 15` | 15(forks ≥ 10k) |
| `w_d` 月下载量 | `log10(downloads+1) / 7 * 30` | 30(downloads ≥ 10M/月) |
| `w_r` releases | `min(release_count, 20) / 20 * 10` | 10(20 个 release) |
| `w_c` close rate | `close_rate * 5` | 5(100% 闭合) |

**缺数据怎么办:**
- 下载量拿不到 → `w_d = 0`(U 上限降到 70,可达但稍低)
- close rate 算不出(无 issues 历史) → `w_c = 0`
- 其它字段缺失视作 0

### 5.3 风险标签(UI 显示,与 T 分独立)

UI 必出的旗子:

| Emoji | 含义 | 触发 |
|---|---|---|
| 🔴 | 已归档 | `archived == true` |
| 🟡 | 仓库太新 | 年龄 < 30 天 |
| 🟡 | 单一维护者 | `contributors_count < 3` |
| 🟡 | 无 LICENSE | `license == null` |
| 🟡 | 长期未维护 | 距上次 push > 365 天 |
| 🟡 | star farming 嫌疑 | stars > 100 且年龄 < 60 天 且 contributors == 1 |
| 🟡 | 没开 issues | `has_issues == false` |
| 🟡 | 无 release | `release_count == 0` 且年龄 > 180 天 |

注意:这些**只是展示标签**,U/T 分有自己的扣分逻辑,不重复算。

## 6. 数据采集细节

### 6.1 元数据(第 ③ 步)

`GET /repos/{owner}/{name}` 单次返回:
- `archived`, `disabled`, `license.spdx_id`, `default_branch`
- `stargazers_count`, `subscribers_count`(=watchers), `forks_count`, `network_count`
- `topics`(array), `has_issues`, `has_wiki`
- `pushed_at`, `created_at`, `updated_at`
- `owner.type`, `owner.login`
- `language`, `description`, `clone_url`, `html_url`, `default_branch`

**额外**:
- `GET /repos/{x}/contributors?per_page=4` → `contributors_count` 至少够判断 ≥3
- `GET /repos/{x}/releases?per_page=1` → 看 Link header 取 total + latest

### 6.2 issue 闭合率(第 ⑤ 步,只对 Top 5)

```
GET /search/issues?q=type:issue+repo:{owner}/{name}+is:closed&per_page=1
→ total_count = closed_count

GET /search/issues?q=type:issue+repo:{owner}/{name}+is:open&per_page=1
→ total_count = open_count

close_rate = closed / (closed + open)  (避免除零;两者都 0 → close_rate = None)
```

注意:`/search/issues` 限速 30/min(authed)。Top 5 × 2 = 10 次,远在限内。

### 6.3 包下载量(第 ⑥ 步,只对 Top 5)

**包名推导规则**(按优先级):
1. 读 cloned `repo/setup.py` 里的 `name=...`(regex)
2. 读 `repo/pyproject.toml` 里 `[project] name` 或 `[tool.poetry] name`
3. 读 `repo/package.json` 里 `"name"`
4. 读 `repo/Cargo.toml` 里 `[package] name`
5. 用 repo 名(小写,replace _ → -)

**注:**第 ⑥ 步发生在第 ⑤ 步之后,但 clone 在 cmd_find 第 ④ 步之后。粗排选完 Top 5 之后,我们只对这 5 个发起 README fetch + 包名推导;真实 clone 仍然只对最终用户选定的那 1 个做。所以包名推导不能依赖 clone,得从 GitHub raw 路径拉:
- `https://raw.githubusercontent.com/{full_name}/{default_branch}/setup.py`
- `pyproject.toml` / `package.json` / `Cargo.toml`

**API 端点**(都免认证):
- Python: `https://pypistats.org/api/packages/{name}/recent` → `data.last_month`
- npm: `https://api.npmjs.org/downloads/point/last-month/{name}` → `downloads`
- Rust: `https://crates.io/api/v1/crates/{name}/downloads` → 求和最近 30 天

任一请求失败或 404 → 该候选 `downloads = None`,U 公式里 `w_d = 0`。

### 6.4 README(第 ⑤ 步)

复用已有 `fetch_readme(full_name, token)`。截断到前 4000 字符喂给 LLM 终排。

## 7. LLM 提示词

### 7.1 改写(第 ①)

```
你在帮一个跨 agent 技能管理工具改写用户的中文需求,以提高 GitHub 搜索召回率。

请把下面这条中文需求改写成 3 个不同角度的英文 GitHub 搜索关键词:
- 角度 1: 按"能力/功能"措辞 (e.g. "remove image background")
- 角度 2: 按"工具/CLI"措辞 (e.g. "image background removal cli")
- 角度 3: 按"技术栈/方案"措辞 (e.g. "rembg python ai")

每个 3-6 个词,纯小写,不要引号、不要标点。

输入: "{用户中文 query}"

只输出一个 JSON 数组 (3 个字符串), 不要任何其他文字:
["...", "...", "..."]
```

### 7.2 粗排(第 ④)

```
用户中文需求: "{原始 query}"

下面是 {N} 个 GitHub 仓库的元数据。请挑出最可能解决用户需求的 5 个,以便下一步深度阅读 README。

候选 (JSON):
[
  {"full_name": "...", "desc": "...", "language": "Python",
   "stars": 1234, "watchers": 56, "forks": 200,
   "U": 67, "T": 85, "flags": ["new repo", "single maintainer"]},
  ...
]

判断时:
- 相关性 > 一切,desc 跟需求毛都不沾的直接跳过
- 同等相关性下,U 高 (有真实用量) 优于 U 低
- T < 30 的尽量避免 (除非相关性远超其他)
- archived 必须排到最后

只输出 JSON 数组 (5 个), 不要其他文字:
[{"full_name": "...", "reason": "<1 句中文,30 字内>"}, ...]
按推荐顺序排。
```

### 7.3 终排(第 ⑧)

```
用户中文需求: "{原始 query}"

下面是 5 个候选的完整数据,含 README 摘录。请综合相关性、真实使用证据、治理透明度,选出 Top 3。

候选 (JSON):
[
  {
    "full_name": "danielgatis/rembg",
    "desc": "...",
    "language": "Python",
    "stars": 18000, "watchers": 320, "forks": 1900,
    "monthly_downloads": 2400000,  // null 表示拿不到
    "release_count": 22,
    "close_rate": 0.92,  // null 表示无历史
    "U": 92, "T": 85,
    "risk_flags": [],
    "readme_excerpt": "...(前 4000 字符)..."
  },
  ... (共 5 个)
]

对每个候选,基于 README 判断它是否真能解决用户需求:
- R (0-10): 相关性。README 里有没有明确对应用户场景的功能/示例?
- recommend_level: "强推" | "推荐" | "谨慎" | "不推荐"
  推荐级别参考:
  - 强推: R ≥ 8, U ≥ 70, T ≥ 70, 无 🔴 flag
  - 推荐: R ≥ 7, U ≥ 30, T ≥ 50, ≤ 2 个 🟡 flag
  - 谨慎: R ≥ 6, 但 U < 30 或 T < 50 或多个 🟡 flag
  - 不推荐: R < 6, 或有 🔴 archived
- why: 2 句中文推荐理由,说清"这库的核心能力是什么、为什么命中用户需求"
- risks: 中文风险点 array,基于 risk_flags + README 看到的隐患

按推荐度从高到低,只输出 JSON,共 3 个:
[
  {
    "full_name": "...",
    "R": 9,
    "recommend_level": "强推",
    "why": "...",
    "risks": ["...", ...]
  },
  ...
]
```

## 8. 输出格式

```
🔎 「{原 query}」 → Top 3

  [0] {strong star emoji} {推荐级别}    {full_name}  ({language})
      R 相关 {R}/10  ·  U 使用 {U}/100  ·  T 治理 {T}/100
      ★ {stars} 👁 {watchers} 🔱 {forks} 📥 {downloads or "无数据"} 📦 {releases} 💬 {close_rate%}
      推荐: {why}
      风险: {risk1}  {risk2}  ...   或   (无)
      装: {install_cmd or "(无标准安装方式)"}     owner ∈ trusted? {是/否}

  [1] ...

  [2] ...

选哪个? 输序号 (或 --yes 默认选 0):
```

## 9. 降级行为

| 缺什么 | 行为 |
|---|---|
| 无 `ANTHROPIC_API_KEY` | 跳过 LLM 改写/粗排/终排;改用 U+T 启发式总分排序(`0.6*U + 0.4*T`);仍输出 Top 3;打印 `[warn] 未设 ANTHROPIC_API_KEY,质量较低,启用 LLM 链路请 export ANTHROPIC_API_KEY` |
| 无 `GITHUB_TOKEN` | 匿名 60/小时;打印 warning(整个 pipeline 大概率撞限速);提示 export 一个 |
| `pypistats.org` 等挂 | 该候选 `downloads = None`,U 公式正常运行,只是这一项为 0 |
| README fetch 失败 | LLM 终排时 `readme_excerpt = ""`,R 分由模型凭 desc 估 |
| 用户加 `--simple` | 强制走老路径(单次 keyword 搜 + stars 排序 + 直接取 [0]) |

## 10. CLI 接口变化

```bash
skillforge find "需求"           # 默认: 新流水线
skillforge find "需求" --simple  # 老路径
skillforge find "需求" --no-readme  # 跳第 ⑤⑥步,只到粗排
skillforge find "需求" --top 5   # 输出多少个 (默认 3)
```

其它现有参数(`--repo`、`--yes`、`--force-new`、`--no-star`、`--install`、`--no-register`、`--copy`)保持不变。

## 11. 模块拆分

新 `skillforge.py` 函数:

| 函数 | 输入 | 输出 |
|---|---|---|
| `llm_rewrite_query(query) -> List[str]` | 中文 query | 3 个英文 query |
| `fetch_metadata(full_name, token) -> dict` | 仓库名 | 第 ③ 步的全部字段 + T 分 + risk_flags |
| `compute_t_score(meta) -> int` | metadata dict | 0-100 |
| `compute_risk_flags(meta) -> List[str]` | metadata dict | 风险标签 array |
| `compute_u_score(meta, downloads, close_rate) -> int` | 各信号 | 0-100 |
| `llm_coarse_rerank(query, candidates) -> List[dict]` | query + N 个候选 | Top 5 (`full_name`, `reason`) |
| `fetch_close_rate(full_name, token) -> Optional[float]` | 仓库名 | 0-1 或 None |
| `guess_package_name(full_name, default_branch, token) -> dict` | 仓库名 | `{ecosystem: "pypi/npm/cargo", name: "..."}` 或 None |
| `fetch_downloads(ecosystem, name) -> Optional[int]` | 包信息 | 月下载量 或 None |
| `llm_final_rank(query, candidates_with_readme) -> List[dict]` | query + 5 候选+ README | Top 3 ranked dicts |
| `render_top3(query, ranked) -> str` | 排序结果 | 用户可读 ASCII |

`cmd_find` 主流程改成串这些函数,旧逻辑挪到 `cmd_find_simple` 作为 `--simple` 入口。

## 12. 边界 / 测试矩阵(实现时回归用)

| 场景 | 期望 |
|---|---|
| 输入纯中文,GitHub 上确实有匹配 | Top 3 全部相关 |
| 输入混 cn-en | 三个改写 query 至少一个搜到相关 |
| 候选含 archived 仓库 | 不出现在推荐 [0],显示 🔴 |
| 候选含 stars > 5000 但仓库 < 10 天 | 显示 🟡 star farming + T 分低 |
| 候选都很小众(stars < 50) | U 分都低,但 R 分能反映匹配度 |
| `ANTHROPIC_API_KEY` 缺失 | 走启发式,有 warning,仍出 Top 3 |
| `GITHUB_TOKEN` 缺失 | 警告 + 大概率撞 rate limit,但不崩 |
| pypistats 挂(网络/404) | 该候选 downloads = None,U 公式略低,流程不断 |
| `find --simple` | 老逻辑,不调用 LLM,不调元数据 API |
| `find --repo owner/name` | 跳过搜索 + 改写,直接进入元数据体检 + 后续流程 |
| 同名 already exist + adopt | 与现有 adoption 流程兼容(发生在第 ⑨ 之后的现有 step) |

## 13. 实现工作量估算

| 模块 | 行数估计 |
|---|---|
| `llm_rewrite_query` | ~30 |
| `fetch_metadata` + `compute_t_score` + `compute_risk_flags` | ~80 |
| `compute_u_score` + `fetch_close_rate` | ~40 |
| `guess_package_name` + `fetch_downloads` | ~80(三个 ecosystem) |
| `llm_coarse_rerank` + `llm_final_rank` | ~80 |
| `render_top3` | ~40 |
| `cmd_find` 重构 + 老路径保留 | ~100 |
| 测试矩阵覆盖 | ~120 |
| **总计** | **~570 行新增**(skillforge.py 当前约 730 行,实现后 ~1300 行) |

## 14. 已确认决策

| # | 决策 | 来源 |
|---|---|---|
| 1 | 中文口语化 query 是主要输入 | 用户回答 Q1 |
| 2 | 走完整 LLM 链路 (~$0.02/find) | 用户回答 Q2 |
| 3 | 永远 Top 3 对比输出 | 用户回答 Q3 |
| 4 | 风险标签只标红不过滤 | 用户回答 Q4 |
| 5 | 引入 stars + watchers + forks + releases + downloads + close_rate 全部 | 用户最新指令 |

## 15. 待 v2 的 backlog(不做)

- 评分权重写到 `~/.skillforge/scoring.toml` 让用户配
- awesome-* 列表 grep(同行背书信号)
- 依赖审计(扫 requirements.txt 是否含已知问题包)
- 自适应改写(第一轮不满意自动再发一组 query)
- StackOverflow 提及数(第三方代理使用度)
- embedding 索引本地技能(`which` 升级)

## 16. 验收标准(spec 通过后,实现验收时跑)

跑下面 5 个查询,人评估每个 Top 3 是否合理:
1. "批量去图片背景"
2. "把 mp4 转成 webm 节省体积"
3. "命令行管理 GitHub PR review 回复"
4. "在浏览器里 OCR 图片"
5. "把代码仓库可视化成依赖图"

每个的 Top 1 必须**相关且不是 awesome 列表**;每个的 R/U/T 三维分必须落在合理区间;archived 仓库必须不出现在 Top 1。
