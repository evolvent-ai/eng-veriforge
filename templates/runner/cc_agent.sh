#!/usr/bin/env bash
set -euo pipefail

: "${VERIFORGE_MODEL_NAME:?VERIFORGE_MODEL_NAME is required}"
: "${VERIFORGE_API_KEY:?VERIFORGE_API_KEY is required}"

task_spec="${VERIFORGE_TASK_SPEC:-01-task/task.yaml}"
roll_dir="${VERIFORGE_ROLL_DIR:-.veriforge-roll}"
output_dir="${VERIFORGE_OUTPUT_DIR:-$PWD/outputs}"
mkdir -p "$roll_dir/claude-config" "$output_dir"

claude_bin="${CLAUDE_BIN:-}"
if [[ -z "$claude_bin" ]]; then
  claude_bin="$(command -v claude || true)"
fi
if [[ -z "$claude_bin" || ! -x "$claude_bin" ]]; then
  printf '%s\n' "未找到 Claude Code CLI。请安装 claude，或设置 CLAUDE_BIN 指定路径。" >&2
  exit 127
fi
if [[ ! -f "$task_spec" ]]; then
  printf '%s\n' "未找到任务定义文件：$task_spec" >&2
  exit 2
fi
if [[ "${VERIFORGE_ADAPTER:-}" != "anthropic_messages" ]]; then
  printf '%s\n' "所选模型与 Claude Code 的 Anthropic 协议不兼容，请改用 Codex CLI。" >&2
  exit 2
fi

prompt_file="$roll_dir/claude-prompt.md"
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

export ANTHROPIC_BASE_URL="${VERIFORGE_CLAUDE_BASE_URL:-${VERIFORGE_BASE_URL:?VERIFORGE_BASE_URL is required}}"
export ANTHROPIC_AUTH_TOKEN="$VERIFORGE_API_KEY"
# Some Claude Code releases still inspect this alias during startup.
export ANTHROPIC_API_KEY="$VERIFORGE_API_KEY"
export ANTHROPIC_MODEL="$VERIFORGE_MODEL_NAME"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$roll_dir/claude-config}"

# 把全部模型别名固定到所选模型。第三方 Anthropic 兼容端点（如 Moonshot）
# 只提供自己的模型 ID，若别名留空，CC 会为后台任务和子 Agent 去调
# 不存在的 Sonnet/Haiku/Opus ID 而静默失败。
export ANTHROPIC_DEFAULT_OPUS_MODEL="$VERIFORGE_MODEL_NAME"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$VERIFORGE_MODEL_NAME"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$VERIFORGE_MODEL_NAME"
export CLAUDE_CODE_SUBAGENT_MODEL="$VERIFORGE_MODEL_NAME"
# 第三方端点尚不支持工具搜索，显式关闭以免请求被拒。
export ENABLE_TOOL_SEARCH=false

# 推理档必须来自 profile，不能写死或留空：留空会让 CC 用默认档，
# 使实际运行档位与 models.yaml 声明的固定参数不一致。
CLAUDE_CODE_EFFORT_LEVEL="$(
  python3 -c 'import json,os,sys; sys.stdout.write(json.loads(os.environ["VERIFORGE_NATIVE_PARAMETERS_JSON"])["output_config"]["effort"])' \
    2>/dev/null
)"
if [[ -z "$CLAUDE_CODE_EFFORT_LEVEL" ]]; then
  printf '%s\n' "无法从 VERIFORGE_NATIVE_PARAMETERS_JSON 解析 output_config.effort" >&2
  exit 2
fi
export CLAUDE_CODE_EFFORT_LEVEL

# 输出上限同样来自 profile。CC 默认 32000、上限 64000；不显式设置会让各模型
# 跑在各自的默认值上，输出被截断的风险不一致，分数因此不可比。
CLAUDE_CODE_MAX_OUTPUT_TOKENS="$(
  python3 -c 'import json,os,sys; sys.stdout.write(str(json.loads(os.environ["VERIFORGE_NATIVE_PARAMETERS_JSON"])["max_tokens"]))' \
    2>/dev/null
)"
if [[ -z "$CLAUDE_CODE_MAX_OUTPUT_TOKENS" ]]; then
  printf '%s\n' "无法从 VERIFORGE_NATIVE_PARAMETERS_JSON 解析 max_tokens" >&2
  exit 2
fi
export CLAUDE_CODE_MAX_OUTPUT_TOKENS

# 上下文窗口必须显式声明。CC 对无法识别的模型默认只按 200K 计算，
# 不带 [1m] 后缀的第三方模型会因此被砍到实际能力的 1/5 并提前触发压缩。
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="${VERIFORGE_CONTEXT_WINDOW:?VERIFORGE_CONTEXT_WINDOW is required}"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="${VERIFORGE_AUTO_COMPACT_LIMIT:?VERIFORGE_AUTO_COMPACT_LIMIT is required}"

# Do not expose runner-internal result paths to the model process. The wrapper
# has already captured the paths it needs above.
unset VERIFORGE_ROLL_DIR VERIFORGE_SCORER_RESULT

exec "$claude_bin" -p "$(<"$prompt_file")" \
  --model "$VERIFORGE_MODEL_NAME" \
  --output-format stream-json \
  --verbose \
  --permission-mode acceptEdits \
  --tools "Read,Write,Edit,Glob,Grep" \
  --no-session-persistence \
  --setting-sources project
