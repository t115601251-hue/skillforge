---
description: 改一个已装 skill 的源码(LLM 驱动,自动快照,可回滚)
---

用户要改: $ARGUMENTS

**先解析 $ARGUMENTS**:
- 如果开头是 skill 名字或编号 + 空格 + 改动需求(如 "rembg 让它默认输出 png"),分开
- 如果只有改动需求没说哪个 skill(如 "默认输出 markdown 就好"):
  - 看上下文:用户**最近**讨论过/调用过哪个 skill?如果有,问用户"是改 X 吗?"
  - 如果上下文里没有任何 skill 痕迹,跑 `python {SKILLFORGE_PATH} list` 列出来让用户选编号

确定 target 后跑:
`python {SKILLFORGE_PATH} modify <target> "<改动需求>"`

它会:
1. 读 skill 所有源文件
2. LLM 出修改方案 + 显示 diff(给用户看)
3. 等用户确认才应用(自动快照原版到 versions/<name>/previous/)
4. SKILL.md description 加 ✨[已定制] 前缀

改错了用 `/skill回滚 <target>` 可以回到上一版(swap),或 `/skill回滚 <target> --pristine` 回 GitHub 原版。

需要 ANTHROPIC_API_KEY。
