#!/usr/bin/env bash
set -euo pipefail

: "${VERIFORGE_MODEL_NAME:?VERIFORGE_MODEL_NAME is required}"
: "${WODEX_API_KEY:?WODEX_API_KEY is required}"

task_spec="${VERIFORGE_TASK_SPEC:-01-task/task.yaml}"
roll_dir="${VERIFORGE_ROLL_DIR:-.veriforge-roll}"
output_dir="${VERIFORGE_OUTPUT_DIR:-$PWD/outputs}"
mkdir -p "$roll_dir/claude-config" "$output_dir"

claude_bin="${CLAUDE_BIN:-}"
if [[ -z "$claude_bin" ]]; then
  claude_bin="$(command -v claude || true)"
fi
if [[ -z "$claude_bin" || ! -x "$claude_bin" ]]; then
  printf '%s\n' "Claude Code CLI not found. Install 'claude' or set CLAUDE_BIN." >&2
  exit 127
fi
if [[ ! -f "$task_spec" ]]; then
  printf '%s\n' "Task spec not found: $task_spec" >&2
  exit 2
fi

prompt_file="$roll_dir/claude-prompt.md"
{
  cat <<'PROMPT'
You are the Agent under evaluation in a VeriForge benchmark.

Work only inside the current isolated workspace. First read the complete task
contract at the path below, then read every fixture it names. The task contract
is authoritative: follow its exact output filenames, JSON keys, enums, event
counts, evidence rules, and safety constraints. Create the required files under
the contract's output directory (normally ./outputs). Do not edit the task
contract, fixtures, runner files, or any path outside the output directory.
Do not use network services, send messages, or make external side effects.
Before finishing, inspect every required output file for valid syntax and
complete required fields. Your final chat response is only a short completion
notice; the files are the deliverable.

Task contract:
PROMPT
  cat "$task_spec"
  cat <<'PROMPT'

Fixture root is normally ./02-evaluation/fixtures. Read it from the task
contract rather than inventing data. Remember that the scorer evaluates the
files on disk, not your final chat response.
PROMPT
} > "$prompt_file"

export ANTHROPIC_BASE_URL="${VERIFORGE_CLAUDE_BASE_URL:-https://api.wodex.ai}"
export ANTHROPIC_AUTH_TOKEN="$WODEX_API_KEY"
# Some Claude Code releases still inspect this alias during startup.
export ANTHROPIC_API_KEY="$WODEX_API_KEY"
export ANTHROPIC_MODEL="$VERIFORGE_MODEL_NAME"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$roll_dir/claude-config}"

exec "$claude_bin" -p "$(<"$prompt_file")" \
  --model "$VERIFORGE_MODEL_NAME" \
  --output-format text \
  --dangerously-skip-permissions \
  --no-session-persistence \
  --setting-sources project
