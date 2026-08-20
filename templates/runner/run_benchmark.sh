#!/usr/bin/env bash
set -euo pipefail

runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
package_root="$(cd "$runner_dir/.." && pwd)"
rolls="${1:-3}"

cd "$package_root"
printf '%s\n' \
  "VeriForge benchmark (Wodex)" \
  "1. 选择 harness：Claude Code (CC) 或 Codex CLI" \
  "2. 选择一个模型" \
  "3. 输入 Wodex API Key（输入内容不会显示或写入文件）" \
  "4. 自动执行 ${rolls} 次隔离 rollout、评分并保存结果" \
  ""
exec python3 03-runner/run_task.py --interactive --rolls "$rolls"
