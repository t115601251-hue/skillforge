#!/usr/bin/env bash
# spec §16 的 5 个验收 query。
# 跑法: GITHUB_TOKEN=xxx ANTHROPIC_API_KEY=yyy bash tests/acceptance_queries.sh
# 每个 query 只显示 Top 3,不真装。Ctrl-C 中断进入下一个。

set -u
cd "$(dirname "$0")/.."

QUERIES=(
  "批量去图片背景"
  "把 mp4 转成 webm 节省体积"
  "命令行管理 GitHub PR review 回复"
  "在浏览器里 OCR 图片"
  "把代码仓库可视化成依赖图"
)

for q in "${QUERIES[@]}"; do
  echo "================================================================"
  echo "Query: $q"
  echo "================================================================"
  PYTHONIOENCODING=utf-8 python skillforge.py find "$q" --force-new --no-star 2>&1 || true
  echo
  read -p "继续下一个? [Enter] " _
done
