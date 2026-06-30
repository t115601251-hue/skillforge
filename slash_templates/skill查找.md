---
description: 自然语言找一个能解决用户需求的 agent skill (agent 做 LLM,skillforge 当工具)
---

用户的需求: $ARGUMENTS

**重要**: 所有 LLM 推理由**你**(正在对话的模型)做,skillforge.py 只当"无脑工具"使。

## 步骤

**1. 先查本地(避免重装)**
跑: `python {SKILLFORGE_PATH} which "$ARGUMENTS"`
如果命中,直接告诉用户用法,结束。

**2. 你自己改写查询(LLM 第一步)**
把用户的中文需求改成 **3 个不同角度**的英文 GitHub 搜索 query:
- 角度 1: 功能/能力 (如 "remove image background")
- 角度 2: 工具/CLI (如 "image background removal cli")
- 角度 3: 技术栈/方案 (如 "rembg python ai")

**3. 跑工具拿候选**
跑: `python {SKILLFORGE_PATH} find-data "<q1>" "<q2>" "<q3>" --top 4 > /tmp/cands.json 2>&1`
- stdout 是纯 JSON(10-15 个候选,带 metadata+T/U/risk_flags)
- 跑完读 `/tmp/cands.json`

**4. 你做粗排(LLM 第二步)**
读 `/tmp/cands.json`,排除明显无关(看 description),挑出 **5 个最可能解决用户需求的** full_name。

**5. 跑工具深读 Top 5**
跑: `python {SKILLFORGE_PATH} deep-data <fn1> <fn2> <fn3> <fn4> <fn5> > /tmp/deep.json 2>&1`
- 拿 README(前 4000 字) + Scorecard 总分 + OSV 漏洞 + 月下载量 + close_rate

**6. 你做终排(LLM 第三步,核心判断)**
读 `/tmp/deep.json`,综合 README + 各分数 + 风险,出 Top 3。
**⚠️ archived 仓库永远不能在 Top 1**,**有 OSV HIGH/CRITICAL 必须把风险写出来**。

**7. 把你的 ranking 写成 JSON 文件**
格式严格如下,写到 `/tmp/ranking.json`:
```json
{
  "query": "<用户原始中文需求>",
  "ranked": [
    {
      "full_name": "owner/repo",
      "R": 9,
      "recommend_level": "强推" | "推荐" | "谨慎" | "不推荐",
      "why": "2 句中文推荐理由,说清楚它的核心能力和为什么命中需求",
      "risks": ["中文风险点 1", "中文风险点 2", ...]
    }
    // 共 3 个,按推荐度排
  ],
  "meta_by_name": { "owner/repo": <从 deep.json 拿来的完整 meta 对象>, ... }
}
```

**8. 渲染并展示**
跑: `python {SKILLFORGE_PATH} render --file /tmp/ranking.json`
把输出**原样**展示给用户。

**9. 问用户怎么办**
"装第几个? 给序号或 owner/repo;不装就说取消。"
用户说装,就调 `/skill安装 <他说的>`。
