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
  printf '%s\n' "未找到 Codex CLI。请安装 codex，或设置 CODEX_BIN 指定路径。" >&2
  exit 127
fi
if [[ ! -f "$task_spec" ]]; then
  printf '%s\n' "未找到任务定义文件：$task_spec" >&2
  exit 2
fi

# CODEX_HOME is per-roll, so no participant global config, MCP, plugin, or
# session state can affect this rollout. The selected provider is configured
# through its OpenAI Responses-compatible endpoint.
provider_id="${VERIFORGE_CODEX_MODEL_PROVIDER:-veriforge-provider}"
provider_base_url="${VERIFORGE_CODEX_BASE_URL:-${VERIFORGE_BASE_URL:?VERIFORGE_BASE_URL is required}}"

# 推理档必须来自 profile，不能写死：写死会让 models.yaml 的固定参数失效，
# 使实际运行档位与矩阵声明不一致，跨模型分数因此不可比。
reasoning_effort="$(
  python3 -c 'import json,os,sys; sys.stdout.write(json.loads(os.environ["VERIFORGE_NATIVE_PARAMETERS_JSON"])["reasoning"]["effort"])' \
    2>/dev/null
)"
if [[ -z "$reasoning_effort" ]]; then
  printf '%s\n' "无法从 VERIFORGE_NATIVE_PARAMETERS_JSON 解析 reasoning.effort" >&2
  exit 2
fi

max_output_tokens="$(
  python3 -c 'import json,os,sys; sys.stdout.write(str(json.loads(os.environ["VERIFORGE_NATIVE_PARAMETERS_JSON"])["max_output_tokens"]))' \
    2>/dev/null
)"
if [[ -z "$max_output_tokens" ]]; then
  printf '%s\n' "无法从 VERIFORGE_NATIVE_PARAMETERS_JSON 解析 max_output_tokens" >&2
  exit 2
fi

context_window="${VERIFORGE_CONTEXT_WINDOW:?VERIFORGE_CONTEXT_WINDOW is required}"
auto_compact_limit="${VERIFORGE_AUTO_COMPACT_LIMIT:?VERIFORGE_AUTO_COMPACT_LIMIT is required}"

# request/stream 重试显式写死，不依赖 CLI 默认值：默认值会随版本变化，
# 会让不同机器上的容错能力不一致，进而影响跨模型分数的可比性。
cat > "$roll_dir/codex-home/config.toml" <<EOF
model_provider = "$provider_id"
model = "$VERIFORGE_MODEL_NAME"
model_reasoning_effort = "$reasoning_effort"
model_max_output_tokens = $max_output_tokens
model_context_window = $context_window
model_auto_compact_token_limit = $auto_compact_limit
disable_response_storage = true

# 沙箱与审批策略。approval_policy = "never" 是这里唯一不依赖 Landlock 的一项：
# 自动批准会把沙箱拦下的命令放行到沙箱外重试，等于给了逃生路，必须断掉。
# writable_roots 留空并排除 /tmp 和 TMPDIR 后，可写根只剩下 workspace 本身。
sandbox_mode = "workspace-write"
approval_policy = "never"

[sandbox_workspace_write]
writable_roots = []
exclude_slash_tmp = true
exclude_tmpdir_env_var = true
network_access = false

[model_providers.$provider_id]
name = "$provider_id"
wire_api = "responses"
requires_openai_auth = true
base_url = "$provider_base_url"
request_max_retries = 4
stream_max_retries = 5
EOF

prompt_file="$roll_dir/codex-prompt.md"
{
  cat <<'PROMPT'
你是 VeriForge benchmark 中被评测的 Agent。

只在当前的隔离工作目录内工作。先完整读取下面给出的任务契约，再读取契约中提到
的每一个 fixture。任务契约是权威依据：严格遵守其中的输出文件名、JSON key、枚举
值、事件数量、证据规则和安全约束。把要求的文件创建在契约指定的输出目录下（通常
是 ./outputs）。不要修改任务契约、fixture、runner 文件，或输出目录之外的任何路
径。不要使用网络服务、发送消息或产生任何外部副作用。结束之前，逐个检查每个必需
的输出文件语法是否正确、必填字段是否完整。你最终的对话回复只是一句简短的完成说
明，真正的交付物是这些文件。

任务契约：
PROMPT
  cat "$task_spec"
  cat <<'PROMPT'

fixture 根目录通常是 ./02-evaluation/fixtures。请从任务契约中读取它，不要自己编
造数据。请记住：scorer 评的是磁盘上的文件，不是你最终的对话回复。
PROMPT
} > "$prompt_file"

export CODEX_HOME="$roll_dir/codex-home"
# Codex CLI custom providers read CODEX_API_KEY. Keep OPENAI_API_KEY for
# clients that use the OpenAI-compatible fallback, but make the Codex-specific
# credential explicit so the request includes Authorization: Bearer <key>.
export CODEX_API_KEY="$VERIFORGE_API_KEY"
export OPENAI_API_KEY="$VERIFORGE_API_KEY"
export CODEX_DISABLE_UPDATE_CHECK=1

# Do not expose runner-internal result paths to the model process. The wrapper
# has already captured the paths it needs above.
unset VERIFORGE_ROLL_DIR VERIFORGE_SCORER_RESULT

exec "$codex_bin" exec \
  --ephemeral \
  --skip-git-repo-check \
  --cd "$PWD" \
  --model "$VERIFORGE_MODEL_NAME" \
  --json \
  - < "$prompt_file"
