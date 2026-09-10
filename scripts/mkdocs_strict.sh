#!/usr/bin/env bash
# 站内链接坏掉只有 `mkdocs build --strict` 看得见 —— ruff / mypy / pytest 都不管。
# 本地没装 docs extra 就跳过（CI 的 docs job 兜底），别拦住不写文档的人提交。
#
# ⚠️ 别加 `--quiet`：它把日志级别压到 ERROR，坏链接的 WARNING 根本不发出来，
# strict 模式就数不到，退出码变成 0 —— 钩子看着在跑，其实什么都拦不住。
# 这一版正是这么写的，靠「故意弄坏一条链接看退出码」才发现。
set -euo pipefail
if command -v mkdocs >/dev/null 2>&1; then
  exec mkdocs build --strict
fi
# conda / venv 里常见：包装好了但可执行文件不在 PATH 上
if python -c "import mkdocs" >/dev/null 2>&1; then
  exec python -m mkdocs build --strict
fi
echo "跳过 mkdocs 检查：本地没装。要装：pip install -e '.[docs]'"
