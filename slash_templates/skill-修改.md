---
description: 改一个已装 skill 的源码 (agent 做 LLM,skillforge 当工具,自动快照可回滚)
---

用户要改: $ARGUMENTS

**重要**: LLM 推理由**你**做,skillforge.py 只做"读源码 → 写盘 → 快照"。

## 解析 target

第一段是 skill name 或编号,后面是需求。例:
- `asset-forge 让它默认输出 png` → target=asset-forge, request=让它默认输出 png
- `1 给加个 --json 选项` → target=1, request=给加个 --json 选项
- `让它默认输出 markdown` → target 没给,看上下文最近调用过的 skill;没有就跑 `python {SKILLFORGE_PATH} list` 让用户选

## 步骤

**1. 拿源码**
跑: `python {SKILLFORGE_PATH} modify-source <target> > /tmp/source.json 2>&1`
读 `/tmp/source.json`,里面有 `{name, skill_dir, files: {path: content}}`。

**2. 你看源码 + 需求,出修改方案(LLM 核心)**
针对用户需求,写一个 **changes 数组**:
```json
[
  {
    "path": "相对路径(如 main.py)",
    "action": "modify" | "create" | "delete",
    "new_content": "完整的新文件内容(modify/create 必填,delete 不需要)"
  },
  ...
]
```

规则:
- **只动需要改的文件**,其它不动
- **SKILL.md 必须保留**,可以微调 description / 触发词反映新行为
- 不要删 LICENSE / README.md
- new_content 是**完整**新文件,不是 diff

**3. 写到文件**
把 changes 数组写到 `/tmp/changes.json`(确保是 valid JSON array)。

**4. 应用**
跑: `python {SKILLFORGE_PATH} modify-apply <target> --file /tmp/changes.json --summary "<用户原始需求>"`
- 它会显 diff —— **⚠️ diff 完整原样转给用户,不要截断省略**,这是他决定 confirm/取消的唯一依据
- 等用户 confirm(或 --yes 跳过)
- 自动快照原版到 `versions/<name>/previous/`
- 应用,SKILL.md 加 ✨[已定制]

**5. 告诉用户**
"改完。想看效果就 try 一下;不满意 `/skill-回滚 <name>` 一键 swap 回原状。"

如果想强制回 GitHub 原版: `/skill-回滚 <name> --pristine`
