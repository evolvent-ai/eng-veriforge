#!/usr/bin/env bash
set -euo pipefail

: "${VERIFORGE_MODEL_NAME:?VERIFORGE_MODEL_NAME is required}"
: "${VERIFORGE_API_KEY:?VERIFORGE_API_KEY is required}"

task_spec="${VERIFORGE_TASK_SPEC:-01-task/task.yaml}"
roll_dir="${VERIFORGE_ROLL_DIR:-.veriforge-roll}"
output_dir="${VERIFORGE_OUTPUT_DIR:-$PWD/outputs}"
mkdir -p "$roll_dir/codex-home" "$output_dir"

codex_bin="${CODEX_BIN:-}"
if [[ -z "$codex_bin" ]]; then
  codex_bin="$(command -v codex || true)"
fi
if [[ -z "$codex_bin" || ! -x "$codex_bin" ]]; then
  printf '%s\n' "Codex CLI not found. Install 'codex' or set CODEX_BIN." >&2
  exit 127
fi
if [[ ! -f "$task_spec" ]]; then
  printf '%s\n' "Task spec not found: $task_spec" >&2
  exit 2
fi

# CODEX_HOME is per-roll, so no participant global config, MCP, plugin, or
# session state can affect this rollout. The selected provider is configured
# through its OpenAI Responses-compatible endpoint.
provider_id="${VERIFORGE_CODEX_MODEL_PROVIDER:-veriforge-provider}"
provider_base_url="${VERIFORGE_CODEX_BASE_URL:-${VERIFORGE_BASE_URL:?VERIFORGE_BASE_URL is required}}"
cat > "$roll_dir/codex-home/config.toml" <<EOF
model_provider = "$provider_id"
model = "$VERIFORGE_MODEL_NAME"
model_reasoning_effort = "xhigh"
disable_response_storage = true

[model_providers.$provider_id]
name = "$provider_id"
wire_api = "responses"
requires_openai_auth = true
base_url = "$provider_base_url"
EOF

prompt_file="$roll_dir/codex-prompt.md"
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

export CODEX_HOME="$roll_dir/codex-home"
export OPENAI_API_KEY="$VERIFORGE_API_KEY"
export CODEX_DISABLE_UPDATE_CHECK=1

exec "$codex_bin" exec \
  --ephemeral \
  --skip-git-repo-check \
  --cd "$PWD" \
  --model "$VERIFORGE_MODEL_NAME" \
  --approve-for-me \
  - < "$prompt_file"
