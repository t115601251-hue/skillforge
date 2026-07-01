---
description: 从零创建一个新 skill (参考 Anthropic skill-creator 设计, agent 起草 + skillforge 落盘)
---

用户想造一个新 skill: $ARGUMENTS

**你**(正在对话的模型)负责起草整个 skill;`skillforge create` 只做无脑落盘。参考 Anthropic 官方 `skill-creator` 设计: <https://github.com/anthropics/skills/tree/main/skills/skill-creator>

## 步骤

### 1. Capture Intent (先摸清用户意图,别急着写)

如果 $ARGUMENTS 已经足够具体(比如"造一个 skill,输入一段中文,输出唐诗风格改写"),可以直接跳到步骤 2。否则先问用户:

1. **这 skill 让 Claude 能做什么?**(一句话说明核心能力)
2. **什么情况下触发它?**(用户说什么样的话/什么上下文会用到)
3. **期望的输出格式?**(纯文本 / Markdown / JSON / 代码 / 文件)
4. **需要 test 用例吗?**(有客观正确答案的 skill 有用;主观风格的可跳过)

用户回答后再往下。

### 2. Interview & Research

- 补问边界:输入格式的极端情况、依赖(需要 MCP/工具吗)、成功标准
- 如果有类似 skill 已装,建议用户 `/skill-列表` 看看是否重复

### 3. 起草 SKILL.md

按 Anthropic skill anatomy 布局:

```
<name>/
├── SKILL.md          必需
├── scripts/          可选 - 可执行代码(确定性任务)
├── references/       可选 - 大段参考资料(按需读)
└── assets/           可选 - 模板/图标/字体等
```

**SKILL.md 结构**:
```markdown
---
name: <kebab-case-name>
description: <一句话:干嘛 + 何时触发。触发词写"pushy"一点,避免 undertrigger>
---

# <显示名>

<核心说明>

## <关键流程/规则,imperative 语气>
```

**关键写法**:
- description 是**唯一触发依据**,同时包含"是什么"和"什么时候用",要够具体
- SKILL.md body 控制在 **500 行内**;更多内容拆到 `references/*.md` 用"see references/xxx.md"引用
- 用 imperative 语气(用"Fetch X"而不是"You should fetch X")
- 有输出格式硬要求就写 `ALWAYS use this exact template:` 段

### 4. 展示草稿, 拿用户确认

把你起草的 SKILL.md 完整展示,附一句:"这样写行吗?我可以改 name / description / body 里任何部分。确认就我落盘。"

用户满意才落盘;要改就 iterate。

### 5. 落盘

把 draft 写成 JSON,存到 `/tmp/create-<name>.json`:

```json
{
  "name": "<kebab-case-name>",
  "files": {
    "SKILL.md": "---\nname: ...\n---\n\n# ...\n\n<full body>"
    // 有其它文件就一起列: "scripts/foo.py": "...", "references/spec.md": "..."
  }
}
```

跑: `python {SKILLFORGE_PATH} create --file /tmp/create-<name>.json`

CLI 会:
- 写到 `~/.skillforge/skills/<name>/`
- 立即写 pristine 快照(创建时的原始版本)
- symlink 到 3 家 agent(Claude Code / Codex / OpenClaw)
- 刷新 CATALOG.md,进列表
- 提示后续可用 `/skill-修改` `/skill-回滚 --pristine` `/skill-卸载`

### 6. 收尾

- 告诉用户:"造好了,跟我说 <触发词> 就会触发"
- 建议:先随便说一句触发它验证一下
- 想改就 `/skill-修改 <name> <怎么改>`(自动快照,可回滚)
- 想撤就 `/skill-卸载 <name>`(数据搬 backups/ 不丢)

## 关键约束

- **不要偷偷落盘**:一定要用户看过草稿点头才写文件
- **不复用已有 name**:cmd_create 检查过如果 `~/.skillforge/skills/<name>/` 已存在会拒;换 name 或让用户先卸旧的
- **description 一定要包含触发词**:否则 agent 不会自动挑到它,skill 就白造了
- **不需要 GitHub token**:纯本地写文件
- **SKILL.md 必须有 YAML frontmatter (`--- ... ---`),含 name + description**:cmd_create 会校验,格式错直接 sys.exit
