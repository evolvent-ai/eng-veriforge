#!/usr/bin/env bash
set -euo pipefail

runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
package_root="$(cd "$runner_dir/.." && pwd)"

cd "$package_root"
printf '%s\n' \
  "VeriForge benchmark" \
  "1. 选择 harness：Claude Code (CC) 或 Codex CLI" \
  "2. 选择一个模型" \
  "3. 选择 rollout 次数" \
  "4. 输入所选模型的 API Key（输入内容不会显示或写入文件）" \
  ""

# 第一个参数可直接指定次数，跳过第 3 步的提问。
if [[ $# -gt 0 ]]; then
  exec python3 03-runner/run_task.py --interactive --rolls "$1"
fi
exec python3 03-runner/run_task.py --interactive
