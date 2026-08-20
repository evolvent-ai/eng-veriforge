#!/usr/bin/env bash
set -euo pipefail

runner_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
package_root="$(cd "$runner_dir/.." && pwd)"
rolls="${1:-3}"

cd "$package_root"
printf '%s\n' \
  "VeriForge benchmark" \
  "1. 选择一个模型" \
  "2. 输入该模型的 API Key（输入内容不会显示或写入文件）" \
  "3. 自动执行 ${rolls} 次 rollout、评分并保存结果" \
  ""
exec python3 03-runner/run_task.py --interactive --rolls "$rolls"
