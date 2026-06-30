# skillforge — 跨 agent 的技能闭环管理工具

> 一句话:用户用自然语言说需求 → 先查本地装没装过能干这事的技能 → 有就告诉他怎么用,没有就去 GitHub 找、点赞收藏、装到本地、自动写成一个能被各 agent 发现的技能。

零第三方依赖,单文件 `skillforge.py`,只用 Python 标准库。已在本地实测跑通(见文末「实测记录」)。

---

## 1. 它解决什么问题

你同时用 Claude Code、Codex 这类编程 agent,它们都认同一种技能格式 `SKILL.md`(开放标准,启动时读每个技能的 `name`+`description`,需求匹配上才加载正文)。但有两个缺口:

1. 一个**普通 GitHub 仓库不是技能**——agent 不会自动把它当能力用。
2. 你不知道**自己到底装过哪些技能**,容易重复找、重复装。

skillforge 把这两件事接成一条闭环,并补上一个「先查本地」的前置闸门。

---

## 2. 闭环流程

```
用户描述需求
     │
     ▼
┌─────────────────────┐
│ 扫本地所有 agent     │   ~/.claude/skills、~/.codex/skills、
│ 技能目录(去重)     │   ~/.openclaw/skills、项目级 .*/skills、权威目录
└─────────┬───────────┘
          ▼
      命中本地?
     ╱        ╲
   是           否
   │             │
   ▼             ▼
告诉用户      GitHub 搜索(按 name+description,star 排序)
已经有了          │
怎么用            ▼
              你确认选哪个
                  │
                  ▼
              ⭐ star + 收藏(需你的 token,先确认)
                  │
                  ▼
              git clone 到权威目录
                  │
                  ▼
              检测安装方式(默认不自动执行陌生代码)
                  │
                  ▼
              生成 SKILL.md(description 写得"主动"以提升触发率)
                  │
                  ▼
              注册:软链到各 agent 目录(一处更新,处处生效)
                  │
                  ▼
          下次再问同样需求 → 直接命中本地,闭环闭合
```

关键设计点:**「写使用教程」和「让技能可被发现」是同一个产物**——生成的 `SKILL.md` 正文就是教程,frontmatter 的 `description` 就是触发器。

---

## 3. 五个命令

```bash
skillforge list                       # 跨 agent 列出已装技能(name 去重 + 同名遮蔽提示)
skillforge which "我想做的事"          # 只查本地:有没有现成技能能干这事
skillforge find  "我想做的事"          # 本地没有就去 GitHub 找并安装(完整闭环)
skillforge trust list|add|remove ...  # 管理"可信 owner 白名单"
skillforge consolidate [--dry-run]    # 把同名物理副本合并到 SKILLFORGE_HOME 改软链
```

`which` 和 `find` 共用同一段「扫本地 → 匹配」逻辑;`find` 在没命中时才往后走。

### find 的常用参数

| 参数 | 作用 |
|---|---|
| `--repo owner/name` | 跳过搜索,直接指定仓库 |
| `--top N` | 搜索候选个数(默认 5) |
| `--yes` | 非交互:自动确认 + 选第一个候选 |
| `--force-new` | 本地已有也强制装新的 |
| `--no-star` | 不点 star |
| `--install` | 允许执行检测到的安装命令(默认**不**自动执行) |
| `--no-register` | 不注册到 agent 目录 |
| `--copy` | 注册用复制而非软链 |

### Adoption(自动发现已有手写 SKILL.md)

`find` 在生成 SKILL.md 前会先在各 agent 目录里看有没有**同名、非软链、description ≥ 80 字符**的 SKILL.md。如果有,说明你自己手写过精品版,默认会**采用它作为权威**(复制到 `SKILLFORGE_HOME`,原目录搬到 `~/.skillforge/backups/`),再软链回各 agent 目录。这保证三个 agent 看到的 description 完全一致,而不是被 skillforge 的模板兜底覆盖。

### Trust 白名单(自动装信任仓库)

```bash
skillforge trust add anthropic microsoft editech-dev   # owner 级,匹配所有该作者仓库
skillforge trust add owner/specific-repo               # 单仓库级
skillforge trust list
skillforge trust remove anthropic
```

`find` 检测到安装命令时,若 owner 在白名单内,等价于自动加了 `--install`(仍会确认,加 `--yes` 才跳过)。文件:`~/.skillforge/trusted.txt`,可手工编辑,`#` 开头行为注释。

### Consolidate(合并同名物理副本)

如果你历史上分别给 `.claude/skills/` 和 `.codex/skills/` 装过同名技能,有 N 份物理拷贝、互不同步。`consolidate` 帮你统一:

```bash
skillforge consolidate --dry-run   # 看影响范围
skillforge consolidate --yes       # 执行:挑权威版(已在 SKILLFORGE_HOME 优先,否则 description 最长),
                                   #       搬到 SKILLFORGE_HOME,其余位置改成软链
```

被替换的原目录都会**整目录搬到 `~/.skillforge/backups/<原名>.<agent>.bak.<unix-ts>/`**,**不留在 agent 扫描目录**(否则 Claude Code / Codex 会把 `.bak` 当成新技能注册,菜单立刻就被污染。这是踩过的真实坑)。

---

## 4. 跨工具自动发现是怎么做到的

不是 skillforge 自己实现的发现引擎,而是**顺着 `SKILL.md` 开放标准**:各 agent 启动时本来就会扫自己的技能目录。skillforge 只做两件事:

1. 把新技能装进一个**权威目录**(`~/.skillforge/skills`)。
2. **软链**到各 agent 目录(`~/.claude/skills`、`~/.codex/skills`、`~/.openclaw/skills`)。

于是 Claude Code / Codex 下次启动就自动发现它。「是否询问使用」由各工具自己的策略控制(Codex 有 `allow_implicit_invocation`、可在 AGENTS.md 写 if/then 路由;Claude Code 可在 SKILL.md 正文里要求"执行前先确认")。

---

## 5. 配置(环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `GITHUB_TOKEN` | 无 | 用于 star/收藏 + 提高搜索限额。**不设置则跳过 star。** |
| `ANTHROPIC_API_KEY` | 无 | 设置后:匹配用 LLM 路由、SKILL.md 用模型生成;不设置则关键词匹配 + 模板生成。 |
| `SKILLFORGE_HOME` | `~/.skillforge/skills` | 装新技能的权威目录 |
| `SKILLFORGE_BACKUPS` | `~/.skillforge/backups` | adoption / consolidate 备份目录(在 agent 扫描路径之外) |
| `SKILLFORGE_TRUSTED` | `~/.skillforge/trusted.txt` | 信任白名单文件 |
| `SKILLFORGE_MODEL` | `claude-sonnet-4-6` | 生成/匹配用的模型 |
| `SKILLFORGE_SCAN_DIRS` | 三个 agent 目录 | 覆盖扫描目录(`:` 分隔) |
| `SKILLFORGE_REGISTER_DIRS` | 三个 agent 目录 | 覆盖注册目标目录 |

匹配与生成都做了**优雅降级**:有 key 用模型,没有就退回纯本地的关键词/模板逻辑,保证零依赖也能跑。

---

## 6. 安全设计(重要)

技能会让 agent 运行指令和代码,社区已出现过供应链攻击。本工具内置这些保护:

- **star、装包默认先确认**,不闷头执行;
- **安装命令默认不自动跑**,先把检测到的命令打印给你看,确认安全后才用 `--install`;
- 生成的 SKILL.md 里附带"使用前请审阅仓库代码与依赖"提示;
- token 只从环境变量读,**绝不硬编码**。

---

## 7. 已知限制 / 待办

- **搜索相关性**:GitHub 搜索按 star 排序时,泛仓库(awesome 列表等)容易霸榜。已改为默认只匹配 `name+description`(实测相关性明显提升);进一步可加 LLM 重排。
- **star 在本工具里没法替你执行**——它改动你的账号,需要你本人持有 token 自己跑(这正是把它做成"你自己运行的 CLI"的原因)。
- **"统一的是/否弹窗"做不到**:各 agent 的确认机制不同,最通用的杠杆是 description(控制何时触发)+ 正文里的确认指令。
- 收藏目前用 star 表示;如需 Star List 分类归档,可再加一层 GraphQL 调用。

---

## 8. 实测记录(沙箱中真实运行)

均为隔离测试目录,未触碰真实账号、未执行任何陌生仓库代码。

**list** — 跨目录扫描 + 软链去重 + 项目级发现:造了 3 个真技能,并把其中一个软链到另一个 agent 目录,`list` 正确识别为 3 个、软链的那个只算 1 次,项目级技能也通过"从当前目录往上找到仓库根"被发现。

**which** — 命中与未命中:
- "extract text from a pdf file" → 命中 `pdf-tools`(匹配度 0.67)
- "help me commit my changes in groups" → 命中 `commit-helper`(0.57)
- "book a flight ticket to tokyo" → 无命中,正确引导去 `find`

**GitHub 搜索**(真实 API 调用):"resize and optimize images" 在只匹配 name+description 后,排前的是 flyimg、Image-Flex 等真正的图片工具。

**find 完整闭环**(用安全的 `octocat/Hello-World`):取元数据 → clone → 检测到无标准安装方式 → 生成 SKILL.md(含主动型 description)→ 软链注册到 3 个 agent 目录,全部成功。

**闭环闭合验证**:装完后再 `which` / `find` 同样需求 → 直接命中本地、`find` 被前置闸门拦下并提示"无需重复安装"。`list` 能看到新技能且去重只算 1 个。

---

## 9. 接下来可以定的几件事

1. 匹配方式:就用现在的「有 key 走 LLM、没 key 走关键词」,还是要加 embedding 这档?
2. 安装策略:保持默认不自动装(更安全),还是给可信仓库开个白名单自动装?
3. 收藏:只 star 够不够,还是要做 Star List 分类?
4. 要不要把它打包成 `pipx install` 能直接用的命令,而不是 `python3 skillforge.py`?

定下来我就接着改。
