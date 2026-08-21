---
name: eng-veriforge
description: >-
  当需要把一个任务构思或既有 Agent 工作流设计、形式化并执行为本地 Agent
  benchmark 时使用，包括生成题干、输出文件契约、标答、评分器、fixture、
  harness allowlist，以及参赛者选择模型的 rollout、固定超参 profile 和多次
  rollout 证据。Use when designing, formalizing, or executing a local Agent
  benchmark task.
---

# VeriForge — 可验证任务工坊

## 目标

VeriForge 是一个独立 Skill，用于把粗略的任务构思或既有的 Agent 工作流，转化
为可在本地验证的 benchmark 任务包。它在同一个任务目录下产出三类产物：

1. **题目定义** —— 题干、目标、初始状态、约束、期望输出文件、输出 schema、
   成功判定标准和已知边界。
2. **评测资产** —— 标答、fixture、rubric、确定性 validator、可选的评判配置，
   以及 scorer。
3. **执行脚本** —— harness 装配、Agent 调用、参赛者从主办方批准的模型中选择
   一个、运行时输入 API Key、固定超参配置、评分，以及 `N` 次 rollout 的证据
   生成。

本 Skill 是 local-first 的。MVP 阶段不上传 ZIP、不创建 Harbor 任务、不运行云
端任务。

## 固定的 benchmark 状态

本 Skill 生成的每个 benchmark 都是完整的活动任务包，必须设置
`task.yaml.status: verified`。参赛者和主办方都不存在撰写或晋级流程。该状态是
固定的任务包标签，不是分数，也不是执行结果；生成的任务包中绝不能出现
`release` 或 `lifecycle` 元数据。

交付前，撰写流程仍然要运行「交付前自检」一节列出的确定性检查：标答与畸形输
出的烟测、依赖与 allowlist 检查、fixture 完整性检查，以及在凭据可用时至少跑
一次本地端到端运行。模型 rollout 失败会作为证据记录下来，但不会改变固定的
benchmark 状态。分数是参赛者和主办方唯一需要跨模型、跨 rollout 次数比较的任
务质量信号。

## 工作流

### 1. 建模任务

先收集或推断，再与用户确认：

- 标题、业务背景、目标和非目标；
- Agent 的输入和初始状态；
- Agent 可以修改的文件、目录或系统；
- 必须产出的输出文件、路径、格式和 schema；
- 成功、部分成功和失败的判定标准；
- 标答来源和可接受的答案差异范围；
- 外部副作用，例如发送、删除、发布、写入、支付或审批；
- 需要的 MCP、CLI、Skill、目录、环境变量和网络。

生成的每个 benchmark 都是面向参赛者的，必须使用
`examples/activity-models.yaml` 中的 canonical matrix。该矩阵有且只有这五个
模型 ID，不允许出现其他 ID：`kimi-k3`、`deepseek-v4-pro`、`qwen3.8-max`、
`claude-opus-5`、`gpt-5.6-sol`。展示名称要与 API ID 分开。参赛者每次运行可以
选择一个模型，但完整矩阵和每个模型的参数都由主办方提供且不可修改。即使是
local-only 的任务，也要连同五个模型一起交付；本地烟测应从这五个 ID 中选一
个，而不是创建单模型例外。

canonical matrix 是混合 provider 的：Claude 和 GPT 走主办方的 Wodex 网关，
Qwen 走主办方提供的阿里云 MaaS endpoint，Kimi 走 Moonshot，DeepSeek 走其官方
endpoint。provider URL、凭据变量名和协议都属于主办方掌握的配置。参赛者只需
为所选模型输入一次隐藏的 API Key，永远不需要选择或配置 provider 和 endpoint。

**协议决定模型能上哪个 harness。**Codex CLI 只接受 Responses 协议（其
`wire_api` 唯一合法值就是 `responses`），CC 只接受 Anthropic Messages 协议。
因此矩阵按协议分成两组：

| harness | 协议 | 模型 |
| --- | --- | --- |
| `cc` | `anthropic_messages` | `claude-opus-5`、`kimi-k3` |
| `codex` | `openai_responses` | `gpt-5.6-sol`、`qwen3.8-max`、`deepseek-v4-pro` |

`kimi-k3` 是其中的例外：Moonshot 的 OpenAI 端点只有 Chat Completions，接到
Codex 上会 404，因此它走 Moonshot 官方的 Anthropic 兼容端点
`https://api.moonshot.cn/anthropic`，只支持 CC。该端点上的模型 ID 是
`kimi-k3[1m]`，与 OpenAI 端点的 `kimi-k3` 不同。这样可以完全避免引入本地协议
转换层。

新增或替换模型时，`adapter` 必须与其 `supported_harnesses` 匹配，否则运行时
必然握手失败。

生成的每个 `models.yaml` 都必须包含
`organizer_controls.model_matrix_locked: true`、
`participant_model_choice_only: true`、`fixed_model_count: 5`、
`fixed_profile_only: true` 和 `credential_mode: per_selected_model`。
`selection.fixed_profile` 必须是 `default`。五个模型各自必须只声明一个 ID 为
`default` 的 profile；不允许出现备用 profile、参赛者可调参数、provider 替换或
未替换的占位符。固定 profile 的参数就是 `reasoning_effort` 和
`max_output_tokens` 两项；推理档取各模型自身支持的最高档（`gpt-5.6-sol` 为
`xhigh`，其余为 `max`），输出上限统一为 `32768`。provider adapter 负责把归一化
的推理档映射到各自原生的字段。

每个模型还必须声明一个非敏感的 `trace` 块，包含 `mode`、`streaming`、
`tool_calls` 和 `normalized_events`。走 Claude/Codex CLI 的模型使用
`native_cli_stream`；直连 Chat 的模型使用有界的 `chat_tool_loop`，让 fixture 读
取和输出写入能通过 allowlist 内的本地工具被观测到。能力标志必须描述真实的传输
行为，而不只是事件采集能力——例如当前的 Chat tool loop 是多轮但非 SSE 的，因此
`streaming: false`。

参赛者工作流只暴露 harness 选择、兼容模型选择、rollout 次数和一次运行时 API
Key。runner 在接受任务包之前，必须校验 canonical 模型 ID 集合完全一致、
provider/adapter/endpoint/base URL 与凭据环境变量映射符合 canonical 定义、
唯一的 `default` profile，以及固定参数。

如果用户只有一个构思，就提出一个或多个可度量的任务版本，指出缺失的证据，然
后生成 `status: verified` 的完整任务包。缺失的本地依赖或凭据只作为运行前准备
项报告，不会引入另一种 benchmark 状态。

### 2. 检查依赖并构建 allowlist

先创建 `03-runner/dependency-manifest.yaml`，再执行只读检查：

- MCP 注册情况或命令可用性；
- CLI 可用性，只在有必要时使用无害的 `--version` 或 `--help` 检查；
- 文件和目录是否存在，以及是否在 workspace 边界内；
- 环境变量只按名称检查是否存在，绝不读取其值；
- 已声明的网络需求；除非用户明确要求做连通性测试，否则不要探测外部系统。

把每项依赖归类为 `ready`、`missing` 或 `unverified`。

向用户展示建议的 allowlist 并请其确认：

- harness 会话可见的 MCP；
- 暴露给 Agent 的 CLI；
- 只读和读写挂载；
- Agent 可见的 Skill；
- 运行时密钥的变量名；
- 允许访问的网络域名。

只有经过确认、确实可用且在 workspace 安全边界内的依赖，才能进入
`harness.allowlist.yaml`。缺失的、未确认的或权限过宽的能力一律不进 harness。

对于默认的本地活动，只建议已确认的 Codex CLI 和 Claude Code（CC）CLI。这两者
都是真实的执行 harness，不是模型 provider；参赛者先选 harness，再选模型。
除非主办方明确确认某项任务需求及其隔离边界，否则不要添加 MCP、额外 Skill、
浏览器、外部应用或网络域名。一个 CLI 只有在通过无害的 `--version` 或
`--help` 检查之后才能进入 allowlist；不要想当然地认为 `cc` 就对应某个特定的
可执行文件名。

如果底层 harness 无法强制执行 allowlist，就报告该隔离限制并停止运行；不要声
称此次运行是隔离的。

### 3. 生成三类产物

使用以下目录结构：

```text
<task-slug>/
├── 01-task/
│   ├── README.md
│   └── task.yaml
├── 02-evaluation/
│   ├── rubric.yaml
│   ├── reference_answer/
│   ├── fixtures/
│   ├── validators/
│   └── scorer.py
├── 03-runner/
│   ├── run_task.py
│   ├── run_benchmark.sh
│   ├── harnesses.yaml
│   ├── cc_agent.sh
│   ├── codex_agent.sh
│   ├── provider_agent.py       # 遗留的开发者专用烟测 adapter
│   ├── isolation-manifest.yaml
│   ├── models.yaml
│   ├── harness.allowlist.yaml
│   └── dependency-manifest.yaml
├── results/
└── evidence/
```

**先用脚手架拷齐骨架，再填空。** 不要手工逐个复制文件：

```bash
python3 scripts/new_task.py <task-slug> --output-dir <父目录> --title "任务标题"
```

它会拷齐全部必需文件、把 `task_id` 统一替换为 `<task-slug>-v1`、设置四个脚本的
可执行权限，并在退出前自检文件齐全性和占位符残留。生成的包开箱即可通过
`--preflight` 和完整 rollout。

脚手架只保证**结构**正确，题干、fixture、标答、判定逻辑和 adapter prompt 仍需
人工填写。下表说明每个文件的处理方式：

| 来源 | 目标 | 处理方式 |
| --- | --- | --- |
| `templates/runner/run_task.py` | `03-runner/run_task.py` | 原样复制，不要修改 |
| `templates/runner/run_benchmark.sh` | `03-runner/run_benchmark.sh` | 原样复制，保留可执行权限 |
| `templates/runner/harnesses.yaml` | `03-runner/harnesses.yaml` | 原样复制 |
| `templates/runner/cc_agent.sh` | `03-runner/cc_agent.sh` | 复制后在 prompt 中追加任务专属的输出契约 |
| `templates/runner/codex_agent.sh` | `03-runner/codex_agent.sh` | 同上 |
| `examples/activity-models.yaml` | `03-runner/models.yaml` | 原样复制，只改 `task_id` |
| `templates/task/01-task/task.yaml` | `01-task/task.yaml` | 按骨架注释逐项填写 |
| `templates/task/02-evaluation/scorer.py` | `02-evaluation/scorer.py` | 保留执行契约与输出 schema，替换维度实现 |
| `templates/task/02-evaluation/rubric.yaml` | `02-evaluation/rubric.yaml` | 按任务调整维度与权重 |
| `templates/task/02-evaluation/validators/*.py` | `02-evaluation/validators/` | 保留函数签名，替换判定逻辑 |
| `templates/task/03-runner/*.yaml` | `03-runner/` | 按实际依赖和路径填写 |

`run_task.py` 已经实现了模型选择、固定 profile 解析、allowlist 校验和隔离控
制。任务专属逻辑只应出现在 adapter 的 prompt 和 evaluation 资产里，不要在别处
重新实现这些机制。

**必需文件、一致性锚点、scorer 输出 schema、validator 接口签名和 adapter 环境
变量表都在 `references/task-contract.md` 中写死。**生成前先读它，生成后逐项核
对。

规则：

- 把 `task.yaml` 作为任务的唯一事实来源。
- 在生成的 README 的标题和一句话任务摘要之后，立刻给出参赛者快速开始。其第
  一条也是主要的命令必须是 `./03-runner/run_benchmark.sh`；说明它会依次提示
  选择 harness、模型和该模型的一个 API Key，然后默认运行并评分三次
  rollout。显式的 `python ... --model` 命令放到进阶／CI 一节。
- 任务 ID 和输出路径在 task、rubric、scorer 和 runner 之间必须完全一致。
- 每个面向参赛者的任务包都必须包含 `harnesses.yaml`、可执行的 `cc_agent.sh`
  和 `codex_agent.sh`，以及确定性的 `02-evaluation/scorer.py`。参赛者 runner
  只解析所选 harness 对应的脚本；`provider_agent.py` 可以作为开发者专用的遗
  留 adapter 保留，但绝不是参赛者运行时的首选。
- 每个面向参赛者的任务包都必须包含 `03-runner/isolation-manifest.yaml`，其中
  声明相对路径形式的 fixture、只读和可写路径。runner 会把
  scorer/标答/rubric/validator 等资产挡在 Agent workspace 之外，从每个 roll
  独立的可信 evaluation 副本执行 scorer，并在每次 roll 前后对 task、fixture、
  scorer、allowlist、dependency、models 和 isolation 控制文件做哈希。
- 每个面向参赛者的任务包都必须实现 `veriforge-trace/v1`，为每次 roll 产出
  `trace/index.json` 和归一化的 `trace/events.jsonl`。索引必须写明是否提供流式
  输出、工具调用和原始 provider 输出。单轮 Chat 运行必须标记为
  `chat_single_turn`，并明确声明无法提供工具调用追踪。
- 优先使用确定性验证；只有当 rubric 明确定义了输入、版本、差异容忍策略和兜底
  行为时，才使用人工或模型评判。
- 只存放已去标识化的 fixture 和证据。
- 拒绝密钥、cookie、口令、生产数据、个人绝对路径，以及范围过宽的 home 目录挂
  载。
- 网络默认为拒绝，外部副作用默认为禁止。
- 当任务向参赛者暴露模型选择时，使用 `veriforge-model-matrix/v2`。矩阵中只能
  包含主办方批准的模型。
- 每个参赛者任务包都必须包含 `organizer_controls`，保持恰好五个 canonical 模
  型，绝不能向参赛者交付未替换的占位符，或者单模型／替代矩阵的变体。
- 把 `selection.mode` 设为 `participant_selects_one`，禁用参赛者的 profile 和
  自定义参数选择，并为每个模型定义同一个固定 profile ID。参赛者选完模型后，
  profile 会被自动选中。

### 4. 生成并运行任务脚本

生成的 runner 必须按以下顺序执行：

```text
预检依赖和运行时密钥
  -> 构建受 allowlist 约束的 harness 会话
  -> 用固定的模型／超参调用 Agent
  -> 校验输出文件
  -> 运行 scorer
  -> 持久化每次 roll 的日志、分数和运行清单
  -> 汇总 模型 × N 次 roll 的结果
```

### adapter 契约交接

任务专属的 Agent adapter 是 benchmark 契约与模型之间的边界。它不能指望模型从
文件名或一段简短的任务摘要里推断出输出格式。runner 通过
`VERIFORGE_TASK_SPEC` 传入任务定义文件的绝对路径；adapter 必须：

1. 把该定义文件复制到隔离的 Agent workspace 中，作为只读的指令文件（例如
   `task.yaml`，并去掉写权限）；
2. 要求 Agent 在读取 fixture 之前先读取这份暂存的定义文件；以及
3. 在 adapter 的 prompt 中逐条复述所有必需的输出文件名、对象 key、枚举值、必
   需章节、证据映射关系和致命约束。字段名必须与确定性 validator 完全一致。
   不要使用近义词，例如用 `requests` 代替 `reviews`，或用 `reason` 代替
   `rationale_code`。

生成的 `cc_agent.sh` 和 `codex_agent.sh` 是参赛者的 harness adapter。它们从隔
离的 roll workspace 中运行，把完整的任务契约传给所选 CLI，要求 Agent 读取暂存
的任务定义文件和 fixture，并把声明的输出写到 `VERIFORGE_OUTPUT_DIR` 下。CC 会
用所选模型经批准的配置来设置 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 和
`ANTHROPIC_MODEL`。Codex 会创建一个临时 `CODEX_HOME`，写入所选模型的
Responses 兼容 provider，并把 Key 注入为 `OPENAI_API_KEY`；它绝不读取参赛者的
全局配置。两个脚本在其 CLI 不可用时都必须给出清晰报错并传播非零退出码。CC 必
须使用其内置的文件／搜索工具 allowlist，而不是
`--dangerously-skip-permissions`；Codex 必须使用该 CLI 支持的自动批准选项
（`--approve-for-me`），在已批准的 CLI 版本中它会提供 `workspace-write` 沙
箱。当该 CLI 版本把两个选项视为互斥时，不要再额外传 `--sandbox`。缺少任何一
个可执行 harness adapter 的任务包都是不完整的。

任何被 scorer 精确比对的字符串字段（例如 rationale code、状态或分类标签），都
必须在 `task.yaml` 和 Agent prompt 中给出封闭的编码表。不要把精确匹配的词表只
藏在标答里；那会让 benchmark 变成规格不足，而不是在测量目标任务能力。

当暂存改变了路径位置时，harness prompt 必须显式说明任务定义文件中的路径与
workspace 中实际路径的映射关系。它还必须把任务定义文件放在 Agent 输出目录之
外，避免 Agent 通过修改契约来「完成」任务。只列出输出文件名，或者把 JSON
schema 留给模型自行发明，都属于不完整的 harness。

交付前要跑一次 schema 烟测，确认标答能通过 scorer，并且一个故意使用替代 key
的畸形输出会以确定性的 schema 错误失败。然后把 adapter 端到端跑一次，把分数
和失败诊断保留在本地 evidence 目录中。

参赛者 runner 必须支持：

```bash
./03-runner/run_benchmark.sh
python 03-runner/run_task.py --preflight
python 03-runner/run_task.py --harness codex --model MODEL_ID --rolls N
python 03-runner/run_task.py --interactive --rolls N
```

shell 包装脚本是参赛者的工作流。它必须在任意当前目录下都能工作，在开始前打印
harness -> 兼容模型 -> API Key -> rollout 这四步流程，并且在参赛者未通过第一
个参数指定其他允许次数时，默认运行三次 roll。Python 命令属于进阶／CI 接口，不
是生成的 README 的主要说明。

如果某个任务在开发者模式下暴露了 adapter 命令，下面两种写法都合法，且行为必须
完全一致：

```bash
python 03-runner/run_task.py --developer-mode --model MODEL_ID --rolls 1 --agent-command ./03-runner/agent.sh
python 03-runner/run_task.py --developer-mode --model MODEL_ID --rolls 1 --agent-command -- ./03-runner/agent.sh
```

runner 为 `--harness cc` 解析 `cc_agent.sh`，为 `--harness codex` 解析
`codex_agent.sh`，并从 Agent workspace 之外每个 roll 独立的可信 evaluation 副
本执行 `02-evaluation/scorer.py`。
`--workspace-source`、`--scorer-command` 和 `--agent-command` 需要显式的
`--developer-mode` 标志，在参赛者模式下会被拒绝。生成的 scorer 从 workspace 之
外的可信只读 evaluation 副本运行。它的 stdout 是唯一权威的 JSON 结果；runner
在校验 scorer 完整性之后，才把该结果写入 roll 的结果文件。

runner 必须为每次 roll 打印 `step 1/6` 到 `step 6/6` 的阶段进度，实时转发经过
脱敏的 Agent stdout/stderr 和 scorer stderr，把完整的 scorer stdout 保留在持久
化日志中，并在子进程仍在运行但没有输出时打印有节制的心跳。Codex 和 Claude
adapter 应该使用它们的结构化事件流，让工具调用、文件操作和命令执行摘要在终端
中可见。runner 还必须报告有节制的失败诊断，并强制执行 profile 的
`timeout_seconds`。把所有子进程输出重定向掉、让参赛者完全看不到进度信号，不是
可接受的 runner 交互体验。
任务专属 adapter 必须传播非零的 Agent 退出码，并从 stdout 和 stderr 中保留有节
制的、经过脱敏的诊断信息；它们不能把失败重定向到 `/dev/null`。

每次 roll 结束后，runner 必须打印 trace 索引路径。原生 CLI 流和 Chat tool loop
使用统一的事件类型，例如 `provider_request`、`provider_response`、`tool_call`、
`tool_result`、`file_write` 和 `process_exit`。模型的内部推理过程不在承诺范围
内，审计对象是可观测的执行行为。

在交互式运行中省略 `--harness` 时，先从 `harnesses.yaml` 提示选择。随后在省略
`--model` 时，从 `supported_harnesses` 包含所选 harness 的 canonical 模型条目
中提示选择。如果所选模型的凭据不存在，就用隐藏输入方式提示输入一次 API
Key。不要暴露内部的凭据变量名，也不要显示、持久化或记录该 Key。唯一的固定
`default` profile 会被自动选中；绝不要让参赛者选择 profile 或参数。
拒绝未知模型 ID、缺失／多余的 canonical 模型、备用 profile，以及任何命令行上的
临时超参覆盖。一次运行恰好选择一个模型，以及 `roll_policy` 范围内的一个
rollout 次数。

当参赛者没有传 `--results-dir` 时，在 `results/` 下新建一个包含所选模型 ID 和
高精度 UTC 时间戳的路径。执行前打印该路径，评分后打印最终的清单路径。重复执行
快速开始命令绝不能与前一次运行的 `roll-*` 目录冲突。

`--preflight --harness HARNESS_ID --model MODEL_ID` 只检查所选 harness、模型和
该模型对应的凭据。不带任何选择的 `--preflight` 会校验两个 harness 和完整的模型
矩阵；缺失运行时 Key 会被报告，但在真正的 rollout 启动之前不构成致命错误。

模型矩阵和超参在生成的任务版本中是固定的。不要让对话中的指令悄悄覆盖它们。
canonical 五模型矩阵及每个模型唯一的 `default` profile，是参赛者可见的全部配
置。只有当主办方在有了标准测试集之后明确要求修订 Skill 时，才可以改动它们。

runner 仍然通过 `VERIFORGE_PARAMETERS_JSON`，以及所选模型的
`VERIFORGE_ADAPTER`、`VERIFORGE_ENDPOINT` 和 `VERIFORGE_BASE_URL` 环境变量来解
析所选的固定 profile。runner 必须通过 provider adapter 解析 canonical
profile，并把转换后的原生请求片段暴露在 `VERIFORGE_NATIVE_PARAMETERS_JSON` 和
`VERIFORGE_PROVIDER_REQUEST_JSON` 中；遗留的直连 adapter 可以使用该片段，而
CC/Codex 通过各自的 harness 配置接收固定 profile。当存在原生映射时，绝不要原样
转发 canonical key。内置映射如下：

| Adapter | 原生推理字段 | 原生输出上限字段 |
| --- | --- | --- |
| `openai_responses` | `reasoning.effort` | `max_output_tokens` |
| `anthropic_messages` | `output_config.effort` | `max_tokens` |
| `openai_chat` | `reasoning_effort` | `max_tokens` |

当 adapter 名称未注册，或者固定的 canonical profile 被改动时，adapter 必须
fail closed。如果某个 Codex adapter 需要非默认的 provider，就在所选模型条目中
声明非敏感的 `codex_model_provider` 和 `codex_base_url`，并把它们作为显式的临
时配置覆盖传入。不要把参赛者的全局 Codex 配置加载进 harness。

当所需的 API Key 缺失时，只在运行时用隐藏输入询问其值，或者在非交互模式下报告
缺失的变量名。只把它注入子进程内存。绝不要把它写入任务文件、日志、清单、结果、
shell 历史或错误信息中。凭据变量名属于 `models.yaml`，其值永远不属于那里。

runner 只能转发 `CODEX_BIN` 和 `CLAUDE_BIN` 这两个具名的非敏感可执行文件覆盖变
量。不要透传参赛者的完整环境。

### 参赛者工作流

参赛者拿到生成的 `verified` 任务包，不修改其状态。他们的工作流只有：

```text
任务构思 -> 题目／评测包 -> 选择 CC 或 Codex -> 选择一个兼容模型
  -> 输入一次 API Key -> 选择 N 次 roll -> 调用所选 harness
  -> 校验并对每次 roll 评分
  -> 查看失败证据 -> 改进 benchmark
```

不存在调试模式和正式模式两套 runner。参赛者可以用不同的允许模型重复同一条命
令，以对比失败模式。所选模型、固定参数、roll 次数和分数必须记录在运行清单中。
清单中每次 roll 的 `status`（`passed`、`failed` 或 `dry_run`）是执行结果；顶层
的 `benchmark_status` 永远是字面量 `verified`。

## 安全与隔离

- 不要自动安装、登录、授权或修复 MCP、CLI 和第三方账号。
- 不要把全局 MCP/Skill 配置、用户的 home 目录或不受限的网络暴露给 harness 会
  话。
- 每次 roll 都从干净的任务包 source 创建新的 workspace。以该 workspace 作为
  Agent 的 cwd 运行，并为每次 roll 分配独立的输出、日志、scorer 结果、HOME、
  配置目录和缓存目录。绝不要把一次 roll 的 workspace 复制到另一次 roll。
- 把 scorer、标答、rubric 和 validator 放在 Agent workspace 之外；fixture 使用
  只读挂载，输出使用按次运行独立的目录。
- 除非用户提供了隔离的测试账号和明确的回滚方案，否则拒绝那些需要真实破坏性或
  不可逆副作用的任务。
- 如果预检报告了必需依赖为 `missing`，不要启动 Agent 运行。
- 不要通过面向参赛者的 runner 暴露未经批准的 provider、模型、profile 或超参覆
  盖。
- 保持 provider 专属的凭据名内部化。只提示输入一次所选模型的 API Key，并只把
  该值注入所选 harness 进程。
- 把 PyYAML 之类的 runner 依赖记录在 `03-runner/dependency-manifest.yaml` 中，
  并在执行前预检。
- 交付前测试凭据提示、`--agent-command` 的两种写法、roll 进度消息、超时处理，
  以及命令缺失时的失败表现。
- 测试 `VERIFORGE_TASK_SPEC` 能到达 adapter、adapter 会把定义文件以只读方式暂
  存，并且 Agent prompt 使用的是 validator 要求的精确输出 key。
- 测试两次 roll 拥有不同的 workspace，且第二次 roll 看不到第一次创建的文件；在
  每个 roll 目录下保留完整的、经过脱敏的 stdout/stderr 日志。
- 测试每个 provider adapter 的 canonical 到原生的参数映射，并把解析出的原生请求
  记录在 roll 清单中。
- 测试两种 harness 选择和 scorer 的自动发现，且不传入任何参赛者命令行覆盖；确保
  `provider_agent.py` 永远不是参赛者的默认选择。
- 测试对任务定义文件、已声明只读路径、受保护控制文件或未声明 workspace 路径的
  修改，会在评分之前导致失败。
- 测试参赛者模式会拒绝 `--agent-command`、`--scorer-command` 和
  `--workspace-source`；开发者模式是唯一允许它们的路径。
- 在把某次 adapter rollout 当作证据之前，先跑完标答烟测和畸形输出 schema 烟测。

## 交付前自检

**先核对一致性锚点。**`references/task-contract.md` 的「一致性锚点」表列出了在
多个文件中被复述、必须逐字一致的取值。逐项比对：

- `task_id` 在 task/rubric/isolation/dependency/allowlist 五处一致；
- 输出路径在 `task.yaml.required_output.path`、`scorer.py` 读取的文件名和
  `isolation-manifest.mutable_paths` 三处一致；
- 输出字段名在 `task.yaml.required_output.schema`、`validators/` 的常量和
  adapter prompt 三处一字不差；
- 精确匹配词表在 `task.yaml.codebooks`、`validators/` 的常量和 adapter prompt
  三处一致；
- 维度 ID 与权重在 `rubric.yaml.dimensions` 和 `scorer.py` 的 `DIMENSIONS` 两处
  一致，权重合计等于 `max_score`；
- 通过线在 `task.yaml.acceptance.minimum_score`、`rubric.pass_policy.min_score`
  和 `scorer.py` 的 `MIN_SCORE` 三处一致；
- fatal 维度在 `rubric.yaml` 和 `scorer.py` 的 `FATAL_DIMENSIONS` 两处一致；
- 只读与可写路径在 `task.yaml.initial_state` 和 `isolation-manifest.yaml` 两处
  一致。

**再跑 scorer 的四个用例**，确认它有判别力而不是无脑给分：

| 输入 | 期望 |
| --- | --- |
| 标答 | 满分、`passed: true` |
| 缺少必填字段或使用替代 key | 低分、`passed: false`，诊断精确指出缺失字段 |
| 违反 fatal 规则但其余正确 | `passed: false`，即使总分高于通过线 |
| 输出文件缺失或非法 JSON | 0 分、`passed: false`，且 scorer 以 0 退出 |

然后逐项确认：

- 「必需文件清单」中的 16 个文件全部存在，4 个脚本可执行；
- 题干和输出文件标准没有歧义；
- 标答和 fixture 可读；
- 每个 rubric 维度都有证据支撑，或被明确标记为人工评判；
- scorer 的路径与输出路径一致；
- 模型 ID、超参、roll 次数上限、并发度、重试次数和结果存放位置都已记录；
- `models.yaml` 恰好包含五个 canonical ID、精确的混合 provider
  adapter/endpoint/凭据映射、每个模型一个 `default` profile，以及固定参数；
- `harnesses.yaml` 恰好声明 `cc` 和 `codex`，凭据只从所选模型条目中取；
- 交互式的「先 harness 后模型」选择流程和 allowlist 拒绝路径都能正常工作；
- 生成的 README 以 `./03-runner/run_benchmark.sh` 开头，包装脚本可执行，且重复
  运行会得到新的结果目录；
- 固定 profile 被自动选中，并与所选模型一起记录；
- allowlist 中只包含已确认且可用的能力；
- 不存在任何密钥、真实客户数据、个人绝对路径或过宽挂载；
- 任务的状态恰好是 `status: verified`，且没有 `release` 或 `lifecycle` 块；
- 每个 harness 都会暂存 `VERIFORGE_TASK_SPEC` 并传递精确的输出契约；
- 每个精确匹配的字符串字段都在任务定义文件中有公开的编码表；
- 每次 roll 都在记录模型哈希和 scorer 哈希的同时记录任务定义文件哈希；
- 每次 roll 都有独立的 workspace、输出目录、日志和 scorer 结果，且 Agent 的 cwd
  就是该 workspace；
- canonical 固定参数被记录下来且不含凭据，所选 harness 收到了经批准的 provider
  配置；
- 标答能通过评分，且使用替代 key 的输出会确定性地失败。

最后报告：任务目标、固定的 `verified` 状态、三类产物路径、依赖状态、allowlist
确认状态、剩余的运行前准备项，以及运行生成任务的命令。
