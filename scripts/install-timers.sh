#!/usr/bin/env bash
# 安装并启用 systemd 用户级定时器。不需要 sudo。
# 可重复执行：文件覆盖写入后自动 daemon-reload + restart。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

mkdir -p "$UNIT_DIR"
say "渲染 systemd 用户单元到 $UNIT_DIR"
for unit in bp-tick.service; do
  sed "s|\${HOME}|$HOME|g" "systemd/${unit}.in" > "$UNIT_DIR/${unit}"
done
cp systemd/bp-tick.timer "$UNIT_DIR/bp-tick.timer"

# 定时器要在"未登录的开机状态"也能跑，必须开 linger。
say "开启 linger（使开机未登录时定时器仍生效）"
loginctl enable-linger "${USER:-$(id -un)}" || say "  警告：linger 开启失败，定时器只在登录会话内有效"

systemctl --user daemon-reload
systemctl --user enable --now bp-tick.timer
say "定时器状态"
systemctl --user --no-pager list-timers bp-tick.timer || true

echo
say "常用维护："
echo "  systemctl --user disable --now bp-tick.timer   # 停用定时任务"
echo "  journalctl --user -u bp-tick.service -n 50     # 看执行日志"
