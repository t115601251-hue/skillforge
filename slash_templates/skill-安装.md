---
description: 装一个 skill(可以是编号、name 或 owner/repo)
---

用户想装: $ARGUMENTS

## 步骤

**0. GitHub Token 检查(v9.4)**

如果 $ARGUMENTS 是**编号**(纯数字,查已缓存的候选)可跳过 token 检查 —— skillforge install 从 last_list.json 拿完整 owner/repo,不需要重新联网。

如果 $ARGUMENTS 是 **owner/repo 或 name**(第一次装/需要拉 README 生成 SKILL.md),要联网,先跑:
`[ -n "$GITHUB_TOKEN" ] && echo HAS_ENV || echo NO_ENV`

**若 NO_ENV 且 gh auth status 也无**,先向用户请求(不要偷偷扫凭据):

```
🔐 装 <target> 需要联网拉 README。请三选一给我 token:

  A) 用 gh 里已登录的 token(推荐,说 "用 gh token" 即可)
  B) 贴 PAT (github.com/settings/tokens, public_repo, 7 天过期)
  C) 取消,不装
```

等用户回复再往下。

---

**1. 跑 install 命令**
跑: `python {SKILLFORGE_PATH} install "$ARGUMENTS" --no-star`

它会:
- $ARGUMENTS 是 owner/repo → 走 simple 路径直装(不调 LLM)
- $ARGUMENTS 是数字 → 查 `/skill-列表` 编号缓存
- 已装的 name → 提示"已经装过了"

安装命令(pip/npm install 等)**默认不跑**,除非 owner 在 ~/.skillforge/trusted.txt 白名单里。

**2. 装完自动 intro**
install 末尾会自动调 `intro` 给个简短介绍。如果你想用更口语的版本:

跑 `/skill-介绍 <刚装的 name>` 让你(agent)亲自写一段。

**3. 提示用户**
"装好了。说'用 <name> 帮我 ...'就能触发。
不喜欢可以 `/skill-修改 <name> <需求>` 改;不要了 `/skill-卸载 <name>`。"
