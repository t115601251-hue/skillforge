---
description: 给某个已装 skill 出一段简短的中文使用说明
---

跑 `python {SKILLFORGE_PATH} intro "$ARGUMENTS"` 把输出转给用户。

输出格式:
```
✨ <name> 装好了
做什么: <一句话>
怎么触发: <agent 该听到什么样的话>
装法: <安装命令(已自动装好)>
```

有 ANTHROPIC_API_KEY 时是 LLM 改写的口语化版,无 key 时是模板版。
