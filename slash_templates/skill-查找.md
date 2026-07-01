---
description: 自然语言找一个能解决用户需求的 agent skill (agent 做 LLM,skillforge 当工具)
---

用户的需求: $ARGUMENTS

**重要**: 所有 LLM 推理由**你**(正在对话的模型)做,skillforge.py 只当"无脑工具"使。

## 步骤

**0. GitHub Token 检查(联网前必做,v9.4)**

跑:`[ -n "$GITHUB_TOKEN" ] && echo HAS_ENV || echo NO_ENV`
若 NO_ENV,再看 gh CLI 是否已登录:`gh auth status 2>&1 | head -3`

**若两者都无 token**,**先停下向用户明确请求**(不要偷偷探测其他凭据存储 —— Claude Code 沙箱会拦):

```
🔐 联网查 GitHub 需要 token,匿名配额 60/小时容易撞满。请三选一:

  A) 用 gh CLI 里已登录的 token (推荐)
     跟我说 "用 gh token",我会跑 GITHUB_TOKEN=$(gh auth token) python ...
     (只在本次会话内生效,不写盘)

  B) 手动给 PAT
     去 https://github.com/settings/tokens → Generate new token (classic)
     只勾 public_repo, 7 天过期
     把 ghp_... 那串贴给我

  C) 不联网,从已装 74 个里挑最接近的
     改走 -skill建议 <需求>,纯本地推荐,不去 GitHub
```

等用户回复再继续。**用户明确说"用 gh token"或贴了 PAT 才能往下走**。

---

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
用户说装,就调 `/skill-安装 <他说的>`。
