#!/usr/bin/env bash
# 首次初始化，一条命令搞定：
#   bash scripts/bootstrap.sh
#
# 全程不需要 sudo、不需要 Docker。uv 自己装到 ~/.local/bin，
# venv 由 uv 创建（绕开 Ubuntu 24.04 缺失 python3-venv 与 PEP 668 的限制）。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# --- 1. uv -------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  say "安装 uv 到 ~/.local/bin（无需 sudo）"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || { echo "找不到 uv，请手动安装后重试"; exit 1; }
say "uv 版本：$(uv --version)"

# --- 2. 虚拟环境 + 依赖 -------------------------------------------------------
say "创建 .venv 并安装依赖"
uv sync --extra dev

# --- 3. 配置文件 --------------------------------------------------------------
if [ ! -f .env ]; then
  say "从 .env.example 生成 .env（不含任何真实密钥）"
  cp .env.example .env
  echo "    请编辑 .env 填入 DEEPSEEK_API_KEY 与 SMTP_PASSWORD 后再运行采集。"
else
  say ".env 已存在，保持不变"
fi

mkdir -p data reports

# --- 4. 数据库 ----------------------------------------------------------------
say "初始化数据库并导入来源清单"
uv run bp init

# --- 5. 自检 ------------------------------------------------------------------
say "环境自检"
uv run bp doctor

# --- 6. 定时器（可选）---------------------------------------------------------
if [ "${INSTALL_TIMERS:-no}" = "yes" ]; then
  say "安装 systemd 用户级定时器"
  bash scripts/install-timers.sh
else
  say "未安装定时器。启用定时执行请运行：INSTALL_TIMERS=yes bash scripts/bootstrap.sh"
fi

say "完成"
cat <<EOF

下一步：
  1. 编辑 .env，填入 DEEPSEEK_API_KEY（和 SMTP_PASSWORD）
  2. 单次采集：      uv run bp run
  3. 校验来源可达：  uv run bp sources check
  4. 预览日报：      uv run bp report --stdout
  5. 发送日报：      uv run bp report --send
  6. 启用定时任务：  INSTALL_TIMERS=yes bash scripts/bootstrap.sh
EOF
