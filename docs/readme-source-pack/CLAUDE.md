# Claude 项目投喂说明

这个 ZIP 是给 Claude 项目使用的 README 视觉素材包。

## 你需要让 Claude 做什么

把 `assets/readme/` 中的 7 张图片合并进项目 README，并保持路径为相对路径。

## 文件夹结构

```text
assets/
└─ readme/
   ├─ 01-hero.png
   ├─ 02-workflow.png
   ├─ 03-core-scenarios.png
   ├─ 04-commands.png
   ├─ 05-mece-categories.png
   ├─ 06-security.png
   └─ 07-github-insert-guide.png
```

## 插图位置建议

1. `01-hero.png`：放在 README 标题、badges 和一句话说明下方。
2. `02-workflow.png`：放在“闭环流程”章节下方。
3. `03-core-scenarios.png`：放在“五种核心场景”章节下方。
4. `04-commands.png`：放在“12 个 slash 命令”章节下方。
5. `05-mece-categories.png`：放在“MECE 5+1 分类”章节下方。
6. `06-security.png`：放在“安全设计”章节下方。
7. `07-github-insert-guide.png`：作为维护说明，可放 README 最后或 docs 中。

## 给 Claude 的操作要求

- 不要把图片转成 base64。
- 不要使用本地绝对路径。
- README 中全部使用 `assets/readme/xxx.png` 相对路径。
- 保留原 README 的文字主体，只在对应章节插入图片。
- 插图之间要留空行，保证 GitHub 渲染美观。
- 图片已做过版面检查，若 Claude 继续编辑图片，请再次检查文字是否出格、错位、压线或被裁切。
