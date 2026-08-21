# VeriForge 任务契约 v1

## 必需文件清单

每个面向参赛者的任务包必须包含以下文件，缺一不可。runner 在加载阶段逐项检
查，并把带 ✓ 的文件纳入前后哈希比对（改动即判 roll 失败）。

| 路径 | 哈希保护 | 说明 |
| --- | :---: | --- |
| `01-task/task.yaml` | ✓ | 唯一事实来源 |
| `01-task/README.md` | | 参赛者快速开始 |
| `02-evaluation/scorer.py` | ✓ | stdout 是唯一权威结果 |
| `02-evaluation/rubric.yaml` | ✓ | 维度与权重 |
| `02-evaluation/reference_answer/` | ✓ | 标答 |
| `02-evaluation/validators/` | ✓ | 确定性校验模块 |
| `02-evaluation/fixtures/` | ✓ | 只读输入数据 |
| `03-runner/models.yaml` | ✓ | canonical 五模型矩阵 |
| `03-runner/harnesses.yaml` | ✓ | 恰好 cc 和 codex |
| `03-runner/harness.allowlist.yaml` | ✓ | 已确认的能力 |
| `03-runner/dependency-manifest.yaml` | ✓ | 依赖与状态 |
| `03-runner/isolation-manifest.yaml` | ✓ | 信任边界声明 |
| `03-runner/run_task.py` | | 从模板复制 |
| `03-runner/run_benchmark.sh` | | 从模板复制，需可执行 |
| `03-runner/cc_agent.sh` | | 需可执行 |
| `03-runner/codex_agent.sh` | | 需可执行 |

`templates/task/` 下提供了可直接复制的骨架，生成任务时应在骨架上填空，而不是
从零编写。

## 一致性锚点

同一份契约在多个文件中被复述，以下取值必须逐字一致，否则任务包不自洽：

| 锚点 | 出现位置 |
| --- | --- |
| `task_id` | `task.yaml`、`rubric.yaml`、`isolation-manifest.yaml`、`dependency-manifest.yaml`、`harness.allowlist.yaml` |
| 输出文件路径 | `task.yaml.required_output.path`、`scorer.py` 读取的文件名、`isolation-manifest.mutable_paths` |
| 输出字段名 | `task.yaml.required_output.schema`、`validators/` 中的常量、adapter prompt |
| 精确匹配词表 | `task.yaml.codebooks`、`validators/` 中的常量、adapter prompt |
| 维度 ID 与权重 | `rubric.yaml.dimensions`、`scorer.py` 的 `DIMENSIONS` |
| 通过线 | `task.yaml.acceptance.minimum_score`、`rubric.yaml.pass_policy.min_score`、`scorer.py` 的 `MIN_SCORE` |
| fatal 维度 | `rubric.yaml` 中 `fatal: true` 的维度、`scorer.py` 的 `FATAL_DIMENSIONS` |
| 只读与可写路径 | `task.yaml.initial_state`、`isolation-manifest.yaml` |

## `task.yaml`

```yaml
schema_version: "veriforge-task/v1"
task_id: "example-task-v1"
title: "一个可度量的 Agent 任务"
status: verified
execution_mode: local_only
objective: "Agent 必须完成的目标"
agent_instructions: "展示给 Agent 的任务说明"
initial_state:
  read_only_paths: []
  mutable_paths: []
required_output:
  path: "outputs/result.json"
  format: json
  schema:
    required_fields: []
# 所有被 scorer 精确比对的字符串字段都必须在此声明封闭词表。
codebooks:
  rationale_code: ["approve", "reject", "needs_info"]
constraints:
  side_effects: forbidden
  network: deny_by_default
  forbidden_actions: []
acceptance:
  minimum_score: 75
  fatal_rules: []
known_limitations: []
```

`status: verified` 是固定的任务包标签。它不由模型分数推断得出，不在 rollout
过程中变化，也不由参赛者修改。`acceptance.minimum_score` 只是单次 Agent 提交
的通过线；分数和执行状态才是运行时结果。作者在交付前会运行确定性烟测以及依
赖／fixture／allowlist 检查，但这些检查不会引入另一种任务状态。

## `rubric.yaml`

```yaml
schema_version: "veriforge-rubric/v1"
task_id: "example-task-v1"
max_score: 100
dimensions:
  - id: output_schema
    weight: 25
    method: deterministic
    validator: validators/check_schema.py
  - id: task_correctness
    weight: 60
    method: deterministic
    validator: validators/check_correctness.py
  - id: safety
    weight: 15
    method: deterministic
    fatal: true
    rule: "不允许出现任何被禁止的副作用。"
pass_policy:
  min_score: 75
  required_dimensions: [task_correctness, safety]
```

## `scorer.py`

### 执行契约

runner 从 Agent workspace 之外的可信 evaluation 副本执行 scorer。生成的 scorer
必须按这个环境编写，不能假设任何其他条件：

| 项 | 取值 |
| --- | --- |
| cwd | evaluation 副本根目录，布局与 `02-evaluation/` 相同 |
| 环境变量 | 只有 `PATH`、`PYTHONDONTWRITEBYTECODE`、`VERIFORGE_OUTPUT_DIR` |
| 读取 Agent 产出 | 只能通过 `VERIFORGE_OUTPUT_DIR` 拼接 |
| 读取 rubric/标答/fixture | 相对 `__file__` 所在目录 |
| 写文件 | 禁止。评分前后 evaluation 副本会被哈希比对，改动即判失败 |
| stdout | 必须且只能是一个 JSON 对象，它是唯一权威结果 |
| 超时 | 复用所选 profile 的 `timeout_seconds` |

runner 不解析 scorer 的 stderr，但会完整落盘，可用于诊断输出。Agent 预先写入
的任何结果文件都会被 runner 覆盖。

### 输出 schema `veriforge-scorer-result/v1`

```json
{
  "schema_version": "veriforge-scorer-result/v1",
  "passed": true,
  "score": 100.0,
  "max_score": 100,
  "dimensions": [
    {"id": "output_schema", "weight": 25, "earned": 25.0, "detail": "schema 校验通过，共 3 条记录"}
  ],
  "reason": "仅在未通过时出现"
}
```

| 字段 | 必需 | runner 用途 |
| --- | :---: | --- |
| `passed` | ✓ | 必须是布尔值。runner 只在它严格等于 `true` 且退出码为 0 时判本次 roll 通过 |
| `score` | ✓ | 写入 roll 记录和运行清单，是唯一可跨模型比较的信号 |
| `schema_version` | ✓ | 固定为 `veriforge-scorer-result/v1` |
| `max_score` | | 与 `rubric.yaml.max_score` 一致 |
| `dimensions` | | 逐维度得分，失败时的主要诊断依据 |
| `reason` | | 未通过原因，通过时省略 |

scorer 必须**永远输出合法 JSON 并以 0 退出**，包括输出文件缺失、JSON 解析失败
或 validator 抛异常这些情况——这些都是 0 分，不是 scorer 故障。若 stdout 不是
JSON 对象，runner 会把该次 roll 记为 `scorer 没有输出 JSON 对象` 并判失败。

### 计分语义

- 每个维度产出 0.0–1.0 的比例，乘以权重得到 `earned`；总分是各维度之和。
- `fatal: true` 的维度 `earned` 为 0 时，整体 `passed` 必须为 `false`，**无论总
  分是否达到通过线**。
- 非 fatal 维度应尽量部分给分，让分数能区分「全错」和「错一半」。

## `validators/`

validator 由 scorer 通过 `sys.path` 导入调用，**不作为独立进程执行**。runner 只
检查目录存在并纳入哈希，从不直接运行它们。统一接口：

| 模块 | 签名 | 返回 |
| --- | --- | --- |
| `check_schema.py` | `check_schema(payload: dict)` | `(bool, str)` — 结构是否合法、人类可读诊断 |
| `check_correctness.py` | `check_correctness(payload: dict, reference: dict)` | `(float, str)` — 0.0–1.0 得分比例、诊断 |
| `check_safety.py` | `check_safety(payload: dict)` | `(bool, str)` — 是否安全、诊断 |

返回的诊断字符串会进入 `dimensions[].detail`，应该具体到可定位问题（例如
`reviews[2].rationale_code 不在编码表中：'maybe'`），不要只说「校验失败」。

职责边界：workspace 越权修改、契约篡改和 fixture 改动由 runner 的哈希校验在评分
之前拦截，**不归 safety validator 管**。后者只检查输出内容本身的违规，例如泄漏
凭据、编造证据、声称执行了被禁止的副作用。

## `reference_answer/`

标答的文件名和结构必须与 `task.yaml.required_output` 完全一致，即「一份满分的
Agent 输出」。scorer 直接读取它做比对，因此它必须能通过自己的 schema 校验。

标答**不是**精确匹配词表的唯一来源。所有被精确比对的取值都必须同时写在
`task.yaml.codebooks` 中并复述进 adapter prompt，否则等于要求 Agent 去猜一套隐藏
的评测约定。

## `harness.allowlist.yaml`

```yaml
schema_version: "veriforge-harness-allowlist/v1"
task_id: "example-task-v1"
confirmed_by_user: false
mcps: []
cli: []
skills: []
mounts: []
environment:
  - name: "PROVIDER_API_KEY"
    required: true
    secret: true
    runtime_only: true
network:
  mode: deny_by_default
  allowed_domains: []
```

每个参赛者任务包还包含 `03-runner/harnesses.yaml`，其中恰好有两个条目：`cc`
（对应 `claude` 可执行文件和 Anthropic 协议）和 `codex`（对应 `codex` 可执行
文件和 OpenAI Responses 协议）。凭据和 base URL 都来自所选模型条目。CC 被限制
在文件／搜索类工具上，runner 为每次 roll 创建独立的配置目录和 home 目录。

## `dependency-manifest.yaml`

```yaml
schema_version: "veriforge-dependency-manifest/v1"
task_id: "example-task-v1"
runtime:
  - name: "python3"
    kind: cli
    check: "python3 --version"
    status: ready
    required: true
  - name: "PyYAML"
    kind: python_package
    check: "python3 -c 'import yaml'"
    version: ">=6.0,<7"
    status: ready
    required: true
harnesses:
  - name: "claude"
    kind: cli
    check: "claude --version"
    executable_env: "CLAUDE_BIN"
    status: unverified
    required: false
credentials:
  - name: "WODEX_API_KEY"
    status: unverified
    runtime_only: true
mcps: []
skills: []
network:
  mode: deny_by_default
  allowed_domains: []
```

`status` 只能是 `ready`、`missing` 或 `unverified`，由生成期的只读检查填写。
`kind` 可用 `cli`、`python_package`、`path` 或 `env`。凭据和环境变量只登记名称，
绝不登记取值；`check` 命令必须是无害的只读探测。

如果预检报告了任何 `required: true` 且 `status: missing` 的条目，runner 不会启动
Agent 运行。

## `models.yaml`

```yaml
schema_version: "veriforge-model-matrix/v2"
policy: fixed_for_task_version

selection:
  mode: participant_selects_one
  allow_participant_model_choice: true
  allow_participant_profile_choice: false
  allow_custom_parameters: false
  max_models_per_run: 1
  fixed_profile: default

models:
  - id: "model-id"
    display_name: "面向参赛者的可读名称"
    provider: "provider-name"
    adapter: "openai_responses"
    model_name: "provider/model-id"
    endpoint: "responses"
    credential_env: "PROVIDER_API_KEY"
    supported_harnesses: ["codex"]
    profiles:
      - id: "default"
        parameters: {}
        timeout_seconds: 1800
        retries: 0

roll_policy:
  min_rolls: 1
  default_rolls: 3
  max_rolls: 10
  max_parallelism: 2
```

`display_name` 是面向参赛者的展示文本；`model_name` 是发送给 provider 的
canonical ID。参赛者每次运行恰好选择一个模型。runner 会自动为该模型使用
`selection.fixed_profile`；profile 和自定义参数都不是参赛者的选项。当
`credential_env` 对应的值缺失时，可以在运行时用隐藏输入录入，并且绝不能被持久
化。

## 参赛者入口

每个生成的任务包都从包根目录暴露一条主要的参赛者命令：

```bash
./03-runner/run_benchmark.sh
```

该包装脚本会切换到包根目录、说明流程，先提示选择 `cc`（Claude Code）或
`codex`，再提示选择一个兼容的 canonical 模型，用隐藏输入索取一次所选模型的 API
Key，然后运行默认次数的 rollout。可选的第一个参数用于修改 rollout 次数。直接
使用 Python 参数属于进阶／CI 接口。如果没有指定结果目录，runner 会创建
`results/<model-id>-<高精度 UTC 时间戳>/`，确保重复运行不会复用之前的 roll
workspace。

对于固定活动，`models.yaml` 中可以包含：

```yaml
organizer_controls:
  model_matrix_locked: true
  participant_model_choice_only: true
  fixed_model_count: 5
  fixed_profile_only: true
  credential_mode: per_selected_model
  approved_model_ids:
    - "kimi-k3"
    - "deepseek-v4-pro"
    - "qwen3.8-max"
    - "claude-opus-5"
    - "gpt-5.6-sol"
```

每个面向参赛者的任务包都必须包含这个块，且 `fixed_model_count: 5`。runner 要求
canonical ID 集合恰好是
`{kimi-k3, deepseek-v4-pro, qwen3.8-max, claude-opus-5, gpt-5.6-sol}`；缺失、多
余或被替换的 ID 都是非法的。runner 还会拒绝 `REPLACE_WITH_*` 占位值、与
`examples/activity-models.yaml` 不一致的 provider/adapter/endpoint/凭据映射，以
及参赛者的 profile 或参数覆盖。活动的凭据模式固定为 `per_selected_model`。
Claude 和 GPT 走 Wodex，Qwen 走主办方提供的阿里云 MaaS endpoint，Kimi 走
Moonshot，DeepSeek 走其官方 endpoint。这个锁定属于任务包层面的策略；赛事分发方
还应发布产物校验和，或以其他方式防止参赛者在分发后修改 `models.yaml`。

### 协议与 harness 的对应

Codex CLI 只接受 Responses 协议（`wire_api` 的唯一合法值是 `responses`），CC 只
接受 Anthropic Messages 协议。因此模型的 `adapter` 必须与其
`supported_harnesses` 匹配：

| 模型 | provider | adapter | endpoint | harness |
| --- | --- | --- | --- | --- |
| `claude-opus-5` | Wodex | `anthropic_messages` | `messages` | `cc` |
| `kimi-k3` | Moonshot | `anthropic_messages` | `messages` | `cc` |
| `gpt-5.6-sol` | Wodex | `openai_responses` | `responses` | `codex` |
| `qwen3.8-max` | 阿里云 MaaS | `openai_responses` | `responses` | `codex` |
| `deepseek-v4-pro` | DeepSeek | `openai_responses` | `responses` | `codex` |

`kimi-k3` 是例外：Moonshot 的 OpenAI 端点只提供 Chat Completions，接到 Codex 上
会 404，因此它使用 Moonshot 官方的 Anthropic 兼容端点
`https://api.moonshot.cn/anthropic`，只支持 CC。**该端点上的模型 ID 是
`kimi-k3[1m]`**，与 OpenAI 端点的 `kimi-k3` 不同；矩阵条目的 `id` 仍是
`kimi-k3`，`model_name` 才是 `kimi-k3[1m]`。这样无需引入任何本地协议转换层。

CC harness 在连接第三方 Anthropic 端点时，还必须把
`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、
`ANTHROPIC_DEFAULT_HAIKU_MODEL` 和 `CLAUDE_CODE_SUBAGENT_MODEL` 一并指向所选模
型，并设置 `ENABLE_TOOL_SEARCH=false`。否则 CC 会为后台任务和子 Agent 去调用第
三方端点上不存在的 Sonnet/Haiku ID 而静默失败。

### 固定 profile 基线

活动矩阵中每个模型只有一个不可变的 `default` profile。固定的活动基线是：

| 模型 | 固定推理档 | 固定输出上限 |
| --- | --- | --- |
| `kimi-k3` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `deepseek-v4-pro` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `qwen3.8-max` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `claude-opus-5` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `gpt-5.6-sol` | `reasoning_effort: xhigh` | `max_output_tokens: 32768` |

推理档的取值是**各模型自身支持的最高档**，而不是统一字面量：`gpt-5.6-sol` 的最
高档是 `xhigh`（它没有 `max` 档），其余四个模型是 `max`。runner 在
`FIXED_REASONING_EFFORT` 中按模型 ID 逐一锁定该取值，`models.yaml` 与之不一致会
被拒绝加载。向 provider 下发它不支持的档位会被拒绝或静默降级，因此不能为了"看
起来统一"而强行都写 `max`。

harness adapter 必须从 `VERIFORGE_NATIVE_PARAMETERS_JSON` 解析实际档位下发
（Codex 写入 `model_reasoning_effort`，CC 设置 `CLAUDE_CODE_EFFORT_LEVEL`），不
得在脚本里硬编码档位——硬编码会让 `models.yaml` 的固定参数失效，实际运行档位与
矩阵声明不一致，跨模型分数因此不可比。

不要向参赛者暴露 provider 专属的预算、temperature、top-p 或备用 profile。在主办
方评估过标准测试集并明确要求修订 Skill 之前，这套五模型基线是唯一的参赛者矩阵。

## `isolation-manifest.yaml`

每个生成的任务包都应在 `03-runner/` 下包含这个文件：

```yaml
schema_version: "veriforge-isolation-manifest/v1"
task_id: "example-task-v1"
fixture_paths:
  - "02-evaluation/fixtures"
read_only_paths:
  - "02-evaluation/fixtures"
  - "01-task/task.yaml"
mutable_paths:
  - "outputs"
```

所有路径都相对于暂存后的 Agent workspace。runner 为每次 roll 创建全新的
workspace，把 scorer/标答/rubric/validator 等资产挡在该 workspace 之外，并从可
信的只读 evaluation 副本执行 scorer。它会在 Agent 运行前后，对每个 fixture、只
读路径、任务定义文件、scorer、allowlist、依赖清单、模型目录和隔离清单做哈希。
任何对受保护控制文件的改动，或对 `mutable_paths` 之外任何 workspace 路径的改
动，都会在评分之前判定该次 roll 失败。`read_only_paths` 和 `mutable_paths` 由本
地兜底逻辑通过前后完整性检查来强制执行；沙箱后端还可以额外以挂载方式强制执行。

## 运行清单

每次 roll 都必须记录：任务 ID／版本、固定的 `benchmark_status: verified`、所选
模型 ID、provider 和 adapter、固定的 canonical 参数、provider 原生参数片段、
roll 总次数和当前序号、开始／结束时间戳、runner 版本、harness allowlist 哈希、
依赖清单哈希、模型和隔离清单哈希、任务定义文件哈希、scorer 哈希、scorer 版本、
执行状态、分数，以及输出路径。它还必须记录 roll workspace、输出目录、
stdout/stderr 日志和 scorer 结果的唯一路径。它不得包含任何密钥值或凭据环境变量
的值。
每次 roll 还必须记录 `trace_mode`、`trace_index`、归一化 trace 事件的路径，以及
流式输出和工具调用的能力标志。trace 索引使用 `veriforge-trace/v1`，它是 roll 结
束后的规范入口。

执行过程中，参赛者终端应该看到带编号的 roll 阶段、经过脱敏的 Agent 流式输出、
scorer 诊断与最终分数，以及子进程仍在运行但没有输出时的周期性心跳。持久化的
stdout/stderr 日志文件才是完整的审计记录；终端渲染只是实时进度视图。
运行结束后，runner 会打印 trace 索引路径。原生 CLI 流和 Chat tool loop 都暴露归
一化的可观测事件；单轮 Chat 兼容路径必须明确声明无法提供工具调用追踪。

canonical profile 会在 Agent adapter 运行之前完成转换。内置的 provider 映射是：

| Adapter | 原生推理字段 | 原生输出上限字段 |
| --- | --- | --- |
| `openai_responses` | `reasoning.effort` | `max_output_tokens` |
| `anthropic_messages` | `output_config.effort` | `max_tokens` |
| `openai_chat` | `reasoning_effort` | `max_tokens` |

转换后的片段通过 `VERIFORGE_NATIVE_PARAMETERS_JSON` 暴露，完整的非敏感请求元数
据通过 `VERIFORGE_PROVIDER_REQUEST_JSON` 暴露。当所选 provider 使用另一种字段形
状时，adapter 不得直接转发 `reasoning_effort` 和 `max_output_tokens`。

## adapter 环境变量

runner 只向 adapter 传递一个最小环境，**不透传参赛者的完整环境**。可用变量：

| 变量 | 内容 |
| --- | --- |
| `VERIFORGE_TASK_SPEC` | 任务定义文件的绝对路径 |
| `VERIFORGE_WORKSPACE` | 本次 roll 的 Agent workspace，同时是 cwd |
| `VERIFORGE_OUTPUT_DIR` | 唯一可写的输出目录 |
| `VERIFORGE_ROLL_DIR` | roll 私有目录，用于临时配置；**不得传给模型进程** |
| `VERIFORGE_FIXTURES_DIR` | fixture 绝对路径 |
| `VERIFORGE_ISOLATION_MANIFEST` | 隔离清单绝对路径 |
| `VERIFORGE_MODEL_ID` / `VERIFORGE_MODEL_NAME` | 矩阵内 ID／发给 provider 的 ID |
| `VERIFORGE_PROVIDER` / `VERIFORGE_ADAPTER` / `VERIFORGE_ENDPOINT` | provider 与协议 |
| `VERIFORGE_BASE_URL` / `VERIFORGE_API_BASE_URL` | provider base URL |
| `VERIFORGE_CODEX_MODEL_PROVIDER` / `VERIFORGE_CODEX_BASE_URL` | Codex 专用 provider 覆盖 |
| `VERIFORGE_HARNESS_ID` / `VERIFORGE_HARNESS_PROTOCOL` | 所选 harness |
| `VERIFORGE_PROFILE_ID` / `VERIFORGE_PARAMETERS_JSON` | 固定 profile 与 canonical 参数 |
| `VERIFORGE_NATIVE_PARAMETERS_JSON` | 转换后的原生参数片段，adapter 应使用这个 |
| `VERIFORGE_PROVIDER_REQUEST_JSON` | 完整的非敏感请求元数据 |
| `VERIFORGE_API_KEY` | 所选模型的 Key，与模型的 `credential_env` 同值 |
| `HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` | 均指向 roll 私有目录 |
| `CODEX_BIN` / `CLAUDE_BIN` | 仅当调用方已设置时转发 |

adapter 在 `exec` 模型进程之前必须 `unset VERIFORGE_ROLL_DIR` 和
`VERIFORGE_SCORER_RESULT`，避免把 runner 内部路径暴露给模型。

## adapter 与输出契约的交接

`task.yaml` 是 Agent 说明和必需输出形状的唯一事实来源。参赛者 runner 把它的绝对
路径暴露为 `VERIFORGE_TASK_SPEC`。任务专属 adapter 必须在隔离的 Agent workspace
中暂存一份去掉写权限的副本，并明确要求 Agent 读取它。如果暂存改变了路径，
adapter 必须说明映射关系（例如 `02-evaluation/fixtures` 映射到 `fixtures/`）。

adapter 的 prompt 必须逐条重复必需的输出文件名、JSON 对象 key、允许的枚举值、必
需的 Markdown 标题、证据引用形式和致命的安全约束。只给文件名是不够的，因为 Agent
可能产出一个看起来合理、却与 validator 不兼容的 schema。adapter 必须在评分之前
拒绝缺失的输出或非零的 Agent 退出码。

默认生成的 adapter 是 `03-runner/cc_agent.sh` 和 `03-runner/codex_agent.sh`。它们
接收一个 provider 无关的运行时 Key，在隔离 workspace 中暂存完整契约，只调用所选
CLI，并把声明的输出写到 `VERIFORGE_OUTPUT_DIR` 下。CC 使用所选模型经批准的
Anthropic 兼容变量；Codex 使用每次 roll 独立的 Responses 兼容 provider 配置。参赛
者 runner 会先调用所选 harness，再调用 `02-evaluation/scorer.py`。如果存在
`provider_agent.py`，它是开发者专用的遗留代码，永远不是参赛者的默认选择。

每个被 scorer 精确比对的字符串字段，都必须在 `task.yaml` 中声明其封闭编码表，并
在 adapter 的 prompt 中重复一遍。精确匹配的取值不能只存在于 `reference_answer/`
中，否则等于要求 Agent 去猜一套隐藏的评测约定。

任务交付前要跑两项确定性烟测：标答必须能通过 scorer；一个故意缺少必填字段或使用
替代 key 的畸形输出，必须以有节制的诊断信息在 schema validator 处失败。
