# eng-veriforge

VeriForge（可验证任务工坊）是一个标准 Agent Skill，用于把任务构思或既有工作流制作成可验证的本地 Agent Benchmark。

> **本仓库是「生成器」，不是 benchmark 本身。**
> 直接在仓库根目录运行 `./03-runner/run_benchmark.sh` 是**无效**的——该路径只
> 存在于生成出来的任务包里。请先按下面的步骤生成一个任务包。

## 快速开始

### 第 1 步：生成任务包骨架

```bash
cd /path/to/eng-veriforge
python3 scripts/new_task.py my-task --output-dir ~/benchmarks --title "我的任务"
```

这会在 `~/benchmarks/my-task/` 下拷齐 18 个必需文件、把 `task_id` 统一设为
`my-task-v1`、给四个脚本加上可执行权限。生成的包**开箱即可运行**，但里面装的
是示例任务（记录审阅），需要替换成你自己的内容。

### 第 2 步：填写任务内容

脚手架退出时会列出 7 项待办，核心是这几个：

| 文件 | 要做什么 |
| --- | --- |
| `01-task/task.yaml` | 题干、输出契约、编码表、约束 |
| `02-evaluation/fixtures/` | 替换成真实的去标识化输入数据 |
| `02-evaluation/reference_answer/` | 写出满分标答 |
| `02-evaluation/validators/*.py` | 保留函数签名，替换判定逻辑 |
| `02-evaluation/rubric.yaml` + `scorer.py` | 对齐维度、权重、通过线 |
| `03-runner/cc_agent.sh` + `codex_agent.sh` | 在 prompt 中复述输出契约 |

完整示例见下面的「跑通一个完整任务」。

### 第 3 步：交给参赛者运行

参赛者拿到任务包后，在**任务包目录**里只需一条命令：

```bash
cd ~/benchmarks/my-task
./03-runner/run_benchmark.sh
```

脚本会依次让参赛者选择 harness（Claude Code 或 Codex）、选择兼容模型、隐藏
选择 rollout 次数（直接回车用默认的 3 次），最后输入该模型的 API Key，然后自动
执行隔离 rollout 和评分。每次运行使用新的
`results/<model>-<timestamp>/` 目录，不需要配置 provider、adapter、scorer、
workspace 或输出路径。

运行前可以先检查环境：

```bash
python3 03-runner/run_task.py --preflight
```

## 跑通一个完整任务

下面用「报销单审阅」这个例子走完全流程。所有命令和输出都可以直接照做。

### 1. 生成骨架

```bash
cd /path/to/eng-veriforge
python3 scripts/new_task.py expense-review --output-dir ~/benchmarks --title "报销单审阅"
cd ~/benchmarks/expense-review
```

### 2. 写输入数据

替换 `02-evaluation/fixtures/records.json`：

```json
[
  {"record_id": "E-001", "amount": 320,   "category": "交通", "receipt": true,  "submitted_days_ago": 3},
  {"record_id": "E-002", "amount": 12800, "category": "设备", "receipt": true,  "submitted_days_ago": 5},
  {"record_id": "E-003", "amount": 780,   "category": "餐饮", "receipt": false, "submitted_days_ago": 2},
  {"record_id": "E-004", "amount": 150,   "category": "交通", "receipt": true,  "submitted_days_ago": 95}
]
```

fixture 必须是去标识化数据，不能含真实姓名、账号或凭据。

### 3. 写题干和判定规则

改 `01-task/task.yaml` 的 `objective` 和 `agent_instructions`：

```yaml
objective: "按公司报销规则审阅每条报销单，给出通过/驳回/待补充的判定"

agent_instructions: |
  读取 fixtures/records.json 中的全部报销单，按下列规则逐条判定，
  把结果写入 outputs/result.json。只使用 fixture 中的数据，不要编造记录。

  判定规则（按顺序匹配，命中即停）：
  1. submitted_days_ago > 90        -> reject      （超过报销时效）
  2. receipt 为 false               -> needs_info  （缺少发票）
  3. amount > 10000                 -> needs_info  （超过审批限额，需上级复核）
  4. 其余                            -> approve
```

**规则必须写死在题干里。**如果判定标准只存在于标答中，等于让 Agent 猜评测约
定，测的就不是任务能力了。同理，`codebooks` 里的枚举值（`approve` /
`reject` / `needs_info`）也必须公开声明。

### 4. 写标答

`02-evaluation/reference_answer/result.json` 就是「一份满分的 Agent 输出」，
结构必须与 `task.yaml.required_output` 完全一致：

```json
{
  "summary": "共审阅 4 条报销单：1 条通过，1 条驳回，2 条待补充材料",
  "reviews": [
    {"record_id": "E-001", "rationale_code": "approve",    "evidence": "fixtures/records.json"},
    {"record_id": "E-002", "rationale_code": "needs_info", "evidence": "fixtures/records.json"},
    {"record_id": "E-003", "rationale_code": "needs_info", "evidence": "fixtures/records.json"},
    {"record_id": "E-004", "rationale_code": "reject",     "evidence": "fixtures/records.json"}
  ]
}
```

按上面的规则逐条核对：E-001 全合规通过；E-002 金额 12800 超限额；E-003 无发
票；E-004 已过 95 天超时效。

### 5. 调整判定逻辑（可选）

骨架自带的三个 validator 已经能处理这个例子。如果你的任务字段名或判定方式不
同，改 `02-evaluation/validators/` 下的三个模块，**保留函数签名不变**：

| 模块 | 签名 | 返回 |
| --- | --- | --- |
| `check_schema.py` | `check_schema(payload)` | `(bool, str)` |
| `check_correctness.py` | `check_correctness(payload, reference)` | `(float, str)` 比例 0.0–1.0 |
| `check_safety.py` | `check_safety(payload)` | `(bool, str)` fatal 维度 |

字段名常量必须与 `task.yaml` 一字不差。

### 6. 跑四个 scorer 用例

这一步验证 scorer 有判别力，而不是无脑给分：

```bash
cd 02-evaluation
mkdir -p /tmp/o/{good,badkey,unsafe,missing}
cp reference_answer/result.json /tmp/o/good/
echo '{"summary":"x","requests":[{"id":"E-001","reason":"approve"}]}' > /tmp/o/badkey/result.json
python3 -c "
import json; d=json.load(open('reference_answer/result.json'))
d['reviews'][0]['evidence']='https://example.com/leak'
json.dump(d, open('/tmp/o/unsafe/result.json','w'), ensure_ascii=False)"

for c in good badkey unsafe missing; do
  printf '%-8s ' "$c"
  VERIFORGE_OUTPUT_DIR=/tmp/o/$c python3 scorer.py
done
cd ..
```

期望结果：

| 用例 | 得分 | passed | 说明 |
| --- | --- | --- | --- |
| `good`（标答） | 100.0 | `true` | scorer 认得对的答案 |
| `badkey`（替代 key） | 15.0 | `false` | 诊断精确指出缺失字段 |
| `unsafe`（证据造假） | 85.0 | `false` | **分数高于通过线但 fatal 维度归零** |
| `missing`（无输出） | 0 | `false` | scorer 仍输出合法 JSON 并以 0 退出 |

第三行是关键：85 分却不通过，这正是 fatal 维度该有的行为。如果四个用例的结果
不是这样，说明 scorer 或 validator 有问题，先修再往下走。

### 7. 核对一致性锚点

同一份契约在多个文件里被复述，以下取值必须逐字一致：

```bash
grep -rn '^task_id:' 01-task 02-evaluation 03-runner   # 应全部相同
```

完整的 8 项锚点见 `references/task-contract.md` 的「一致性锚点」表，
包括输出路径、字段名、枚举词表、维度权重、通过线、fatal 维度等。

### 8. 预检

```bash
python3 03-runner/run_task.py --preflight
```

输出示例：

```text
cc：harness 可用（claude）
codex：harness 可用（codex）
claude-opus-5：凭据 缺失（WODEX_API_KEY）
...
```

凭据「缺失」不是错误——运行时会用隐藏输入提示录入。但 harness「缺失」需要先装
对应的 CLI。

### 9. 正式运行

```bash
./03-runner/run_benchmark.sh
```

依次选 harness、选模型、选 rollout 次数（1–10，直接回车用默认的 3 次），最后输入
该模型的 API Key。

也可以用参数直接指定次数、跳过第 3 步的提问：

```bash
./03-runner/run_benchmark.sh 5
```

每次 roll 打印 6 步进度，结束时给出：

```text
[veriforge] 第 1/2 次 roll：得分=100.0 状态=passed
[veriforge] 第 2/2 次 roll：得分=100.0 状态=passed
[veriforge] 完成：2 次 roll 中通过 2 次
[veriforge] 运行清单: results/claude-opus-5-<时间戳>/run-manifest.json
```

### 10. 查看结果

```text
results/<model>-<时间戳>/
├── run-manifest.json          汇总：模型、分数、哈希、trace 元数据
├── roll-1/
│   ├── scorer-result.json     逐维度得分与诊断
│   ├── workspace/             该次 roll 的独立工作目录
│   │   └── outputs/           Agent 的产出（唯一可写位置）
│   ├── logs/                  脱敏后的 stdout/stderr
│   ├── trace/                 归一化事件流（veriforge-trace/v1）
│   └── home/ config/ cache/   roll 私有的环境目录
└── roll-2/                    完全独立，不受 roll-1 影响
```

失败时先看 `roll-N/scorer-result.json` 的 `dimensions[].detail`，它会指出具体
是哪个字段、哪条记录出的问题。

## 作为 Claude Code Skill 使用

如果希望由 Claude 帮你完成任务设计（而不是自己填空），把仓库软链到 skill 目录：

```bash
ln -s /path/to/eng-veriforge ~/.claude/skills/eng-veriforge
```

之后直接描述你想做的评测任务，Claude 会按 `SKILL.md` 的流程与你确认需求、调用
脚手架、生成完整任务包并执行自检。

## 三类产物

1. 题目定义：题干、初始状态、约束和输出文件标准。
2. 评测资产：标答、fixture、rubric、验证器和评分器。
3. 执行脚本：隔离 harness、Agent 调用、固定模型/超参、评分和 `N` 次 roll 聚合。

生成的 participant 包必须同时包含 CC/Codex harness adapter、可信的
OpenAI Chat Completions adapter、deterministic scorer 和 isolation manifest；
参赛者不需要配置这些内部组件。Chat 模型始终走 `/chat/completions`，不会
被错误地送到 Codex 的 `/responses` 协议。

任务契约必须公开每个 JSON 字段的精确类型，安全 validator 必须区分实际的
危险动作和明确拒绝该动作的审计文字；不能用会把否定句误判为危险动作的全局正则。

skill 生成的每个任务包都直接是可运行的 `status: verified` 包。状态是固定
的包元数据，不是参赛者或主办方需要流转的流程；rollout 分数和执行结果是
唯一需要比较的运行指标。任务包不包含 `release` 或 `lifecycle` 配置。

## 当前边界

- 单一 Skill，不拆分 Skill2。
- skill 会在生成时完成契约、fixture、validator、scorer 和 runner 的装配，
  并将任务固定为 `verified`；依赖或凭据缺失只作为运行前准备项报告。
- 运行前只读检查 MCP、CLI、路径和环境变量。
- 只有用户确认且检查通过的能力才进入 harness allowlist。
- API Key 只在运行时注入，不写入任务包或日志。
- 当前版本使用主办方锁定的混合 provider 矩阵，不包含云端 ZIP 上传、Harbor 调度或凭据托管。

详细契约见 `references/task-contract.md`。

## 参赛者固定模型矩阵

所有生成的 participant-facing benchmark 都直接使用
`examples/activity-models.yaml` 的 canonical matrix：`kimi-k3`、
`deepseek-v4-pro`、`qwen3.8-max`、`claude-opus-5`、`gpt-5.6-sol`。无论任务
是否 `local_only`，任务包都必须包含这五个模型，不能生成单模型例外或替换
模型。五个模型各只有一个固定 `default` profile，推理档取各自的最高档
（`gpt-5.6-sol` 为 `xhigh`，其余为 `max`），输出
上限统一为 `32768`。runner 会拒绝缺失/额外/未知模型、额外 profile、参数
不完整、provider 映射不一致或仍含 `REPLACE_WITH_*` 的配置。

模型按协议分成两组，因为 Codex CLI 只接受 Responses 协议，CC 只接受 Anthropic
Messages 协议：

| harness | 模型 |
| --- | --- |
| Claude Code (CC) | `claude-opus-5`、`kimi-k3`、`qwen3.8-max`、`deepseek-v4-pro` |
| Codex CLI | `gpt-5.6-sol` |

除 `gpt-5.6-sol` 外的四个模型都走各自 provider 的 Anthropic 兼容端点，所以只支
持 CC。这样不需要引入任何本地协议转换层（如 CC Switch）。

参赛者拿到任务包后不需要填写 provider、endpoint、模型 ID 或任何超参，先选择
CC 或 Codex，再从该 harness 的兼容模型中选择一个，在运行时输入一次所选模型
的 API Key，并选择 rollout 次数。runner 会在内部处理 Wodex、阿里云 MaaS、
Moonshot 和 DeepSeek 的配置差异。

默认能力基线建议只包含：

- Codex CLI harness；
- Claude Code（CC）CLI harness（可用 `CLAUDE_BIN` 指定路径）；
- Python 3 和 PyYAML 作为 runner 依赖。

默认不启用 MCP、额外 Skill、浏览器、外部应用或额外网络。CC/Codex 是执行
harness，不是 `models.yaml` 中可以由参赛者自由添加的 provider；provider 和
endpoint 都由主办方锁定。
只有完成 `--version`/`--help` 等只读检查并经主办方确认的 CLI，才能写入
`harness.allowlist.yaml`。

## 参赛者模型选择

活动包在 `03-runner/harnesses.yaml` 和 `03-runner/models.yaml` 中声明两个
harness、五个允许模型和固定超参。参赛者每次先选择 harness，再选择一个
模型和 rollout 次数，runner 自动使用该模型唯一的 `default`
profile。使用 `veriforge-model-matrix/v2` 时，runner 支持交互式选择：

```bash
./03-runner/run_benchmark.sh
```

需要指定 rollout 次数时，例如运行 5 次：

```bash
./03-runner/run_benchmark.sh 5
```

CI 或高级调用也可以显式指定模型：

```bash
python 03-runner/run_task.py \
  --harness codex \
  --model gpt-5.6-sol \
  --rolls 3
```

开发调试时如果提供自定义 Agent adapter，可以直接传入命令；下面两种分隔符写法等价：

```bash
python 03-runner/run_task.py --developer-mode --model MODEL_ID --rolls 1 --agent-command ./03-runner/agent.sh
python 03-runner/run_task.py --developer-mode --model MODEL_ID --rolls 1 --agent-command -- ./03-runner/agent.sh
```

`--agent-command`、`--scorer-command` 和 `--workspace-source` 只在显式
`--developer-mode` 下可用；participant workflow 会拒绝这些 override。

runner 会为每个 roll 打印 `step 1/6` 到 `step 6/6` 的阶段进度，并实时转发
经过脱敏的 Agent stdout/stderr 和 scorer stderr；scorer 的完整 stdout 保存在日志文件，
终端只显示最终分数，避免完整 JSON 淹没 Agent 过程。如果子进程暂时没有输出，每 15 秒打印
一次仍在运行的心跳。Codex/Claude harness 使用结构化事件流，因此终端会显示
工具调用、文件操作和命令执行摘要。runner 使用 profile 中的 `timeout_seconds`
限制单次 Agent 运行。所选模型的 API Key 只在运行时隐藏输入
或从内部映射的环境变量读取，绝不会写入配置、manifest 或错误日志。Agent adapter 必须
传播非零退出码，并保留经过脱敏的有限 stdout/stderr 诊断，不能把失败
重定向到 `/dev/null`。Codex harness 会为每个 roll 注入所选模型的临时 provider
配置，不能加载参与者的全局 Codex 配置；CC harness 使用已批准的 Anthropic
兼容配置和 `Read/Write/Edit/Glob/Grep` 文件工具白名单。

每个 roll 还会生成 `trace/index.json` 和统一的 `trace/events.jsonl`，并在
终端打印 trace index 路径。Claude/Codex 原生 CLI 使用
`native_cli_stream`；Qwen、Kimi、DeepSeek 使用受限的 `chat_tool_loop`，只
允许列出/读取 fixture 和写入 outputs。trace 记录请求、响应、工具调用、工具
结果和文件写入；不会承诺或保存模型隐藏推理。模型矩阵中的 `trace` 能力字段
描述实际 wire 行为，Chat tool loop 当前是多轮非 SSE 流式。

每个 roll 都从干净的任务包 source 创建独立 workspace，并以该目录作为
Agent 的 cwd。`02-evaluation/scorer.py`、`rubric.yaml`、`reference_answer/`
和 `validators/` 不会进入 Agent workspace；runner 会在 workspace 外创建独立的
只读 evaluation 副本，并从该可信副本执行 scorer。`outputs`、stdout/stderr
日志、scorer result、HOME、配置和缓存目录也都按 roll 分开。前一个 roll
在 workspace 中创建或修改的文件不会进入后一个 roll。runner 会按选择自动调用
`03-runner/cc_agent.sh` 或 `03-runner/codex_agent.sh`，并从可信 evaluation
副本发现 `02-evaluation/scorer.py`，参赛者不需要传入 adapter 或 scorer 命令。
`--workspace-source`、`--scorer-command` 和
`--agent-command` 仅在显式 `--developer-mode` 下作为开发调试 override，并通过
scorer stdout 生成当前 roll 的结果文件；Agent 不能预写结果来覆盖可信 scorer 输出。

固定 profile 的统一参数不会直接冒充厂商参数。runner 内置 provider
adapter，并将转换后的请求片段放在 `VERIFORGE_NATIVE_PARAMETERS_JSON`
和 `VERIFORGE_PROVIDER_REQUEST_JSON`：`openai_responses` 使用
`reasoning.effort`/`max_output_tokens`，`anthropic_messages` 使用
`output_config.effort`/`max_tokens`。任务 adapter 必须使用转换后的字段。

生成包中的 `03-runner/isolation-manifest.yaml` 声明 fixture、只读和可写
路径。runner 会在每个 roll 前后校验所有 read-only 路径、fixture、task spec
以及 workspace 中所有非 mutable 路径；发现 Agent 修改契约、输入 fixture
或任何未授权路径时，该 roll 会在评分前失败。scorer、task、fixtures、
allowlist、dependency、models 和 isolation manifest 的完整哈希会写入
run manifest。

runner 会把任务定义文件的绝对路径通过 `VERIFORGE_TASK_SPEC` 传给 adapter。
adapter 必须把它复制到隔离工作目录，要求 Agent 先读取该文件，并在 prompt
中明确列出 validator 要求的精确字段名、枚举、证据引用格式和 Markdown
标题。只列出输出文件名，或让 Agent 自行发明 JSON key，不符合 VeriForge
契约。分发前应让 reference answer 通过 scorer，并让一个缺少必填字段或
使用替代 key 的输出以确定性 schema 错误失败。

如果另一台机器上的 Codex 可执行文件不在默认路径，adapter 可以使用
`CODEX_BIN`；runner 只会显式转发这个已知的非敏感变量，不会透传完整用户
环境。

参赛者只能选择 canonical matrix 中声明的模型，不能通过命令行临时覆盖
temperature、top_p、max token 或 reasoning 参数。选择模型后，若对应的
API Key 环境变量不存在，runner 会用隐藏输入提示输入 Key；Key 只注入
当前 rollout 进程，不会写入文件、日志或 manifest。

`--preflight --model MODEL_ID` 只检查所选模型的 credential env；不带
`--model` 的 preflight 会校验完整矩阵并展示五个 credential env 的状态，
但不会因为未选择的模型缺少 Key 而失败。

固定参数只有两项：`reasoning_effort: max` 和
`max_output_tokens: 32768`。不要在生成的任务包中暴露替代 profile、参数
菜单或“推荐值/可选值”说明。后续只有主办方明确要求修改 skill 时，才更新
这组活动基线。

runner 模板使用 Python 3 和 PyYAML 读取 `models.yaml`；生成任务时应在
`03-runner/dependency-manifest.yaml` 中记录该依赖并进行预检。
