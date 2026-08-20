# eng-veriforge

VeriForge（可验证任务工坊）是一个标准 Agent Skill，用于把任务构思或既有工作流制作成可验证的本地 Agent Benchmark。

## 参赛者一键运行

进入生成的 benchmark 目录，只运行：

```bash
./03-runner/run_benchmark.sh
```

脚本会依次让参赛者选择一个模型、隐藏输入该模型的 API Key，然后默认自动
执行 3 次 rollout 和评分。每次运行使用新的 `results/<model>-<timestamp>/`
目录，不需要配置 provider、adapter、scorer、workspace 或输出路径。

## 三类产物

1. 题目定义：题干、初始状态、约束和输出文件标准。
2. 评测资产：标答、fixture、rubric、验证器和评分器。
3. 执行脚本：隔离 harness、Agent 调用、固定模型/超参、评分和 `N` 次 roll 聚合。

生成的 participant 包必须同时包含 task-specific provider adapter、
deterministic scorer 和 isolation manifest；参赛者不需要配置这些内部组件。

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
- 当前版本本地优先，不包含云端 ZIP 上传、Harbor 调度或凭据托管。

详细契约见 `references/task-contract.md`。

## 参赛者固定模型矩阵

所有生成的 participant-facing benchmark 都直接使用
`examples/activity-models.yaml` 的 canonical matrix：`kimi-k3`、
`deepseek-v4-pro`、`qwen3.8-max`、`claude-opus-5`、`gpt-5.6-sol`。无论任务
是否 `local_only`，任务包都必须包含这五个模型，不能生成单模型例外或替换
模型。五个模型各只有一个固定 `default` profile，推理档统一为 `max`，输出
上限统一为 `32768`。runner 会拒绝缺失/额外/未知模型、额外 profile、参数
不完整、provider 映射不一致或仍含 `REPLACE_WITH_*` 的配置。

参赛者拿到任务包后不需要填写 provider、endpoint、模型 ID 或任何超参，只能
从这五个已批准模型中选择一个，在运行时输入所选模型的 API Key，并选择
rollout 次数。固定使用 `per_selected_model`：每次只输入和注入当前选中模型
对应的一个 Key；其他四个模型的 Key 缺失不应阻止当前运行。

默认能力基线建议只包含：

- Codex CLI adapter；
- Claude Code（CC）CLI adapter，具体命令名和版本由主办方确认；
- Python 3 和 PyYAML 作为 runner 依赖。

默认不启用 MCP、额外 Skill、浏览器、外部应用或网络。CC/Codex 是执行
adapter 的 CLI 能力，不是 `models.yaml` 中可以由参赛者自由添加的 provider。
只有完成 `--version`/`--help` 等只读检查并经主办方确认的 CLI，才能写入
`harness.allowlist.yaml`。

## 参赛者模型选择

活动包在 `03-runner/models.yaml` 中声明五个允许模型和固定超参。参赛者每次
只选择一个模型和 rollout 次数，runner 自动使用该模型唯一的 `default`
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
  --model gpt-5.6-sol \
  --rolls 3
```

任务如果提供 Agent adapter，可以直接传入命令；下面两种分隔符写法等价：

```bash
python 03-runner/run_task.py --model MODEL_ID --rolls 1 --agent-command ./03-runner/agent.sh
python 03-runner/run_task.py --model MODEL_ID --rolls 1 --agent-command -- ./03-runner/agent.sh
```

runner 会在每个 roll 开始和结束时打印非敏感进度，并使用 profile 中的
`timeout_seconds` 限制单次 Agent 运行。API Key 仍只在运行时隐藏输入或从
环境变量读取，绝不会写入配置、manifest 或错误日志。Agent adapter 必须
传播非零退出码，并保留经过脱敏的有限 stdout/stderr 诊断，不能把失败
重定向到 `/dev/null`。如果 Codex 使用非默认 provider，可在 `models.yaml`
中声明非敏感的 `codex_model_provider` 和 `codex_base_url`，由 adapter 注入
临时配置，不能加载参与者的全局 Codex 配置。

每个 roll 都从干净的任务包 source 创建独立 workspace，并以该目录作为
Agent 的 cwd；`outputs`、stdout/stderr 日志、scorer result、HOME、配置和
缓存目录也都按 roll 分开。前一个 roll 在 workspace 中创建或修改的文件
不会进入后一个 roll。runner 会自动发现生成包中的
`03-runner/provider_agent.py` 和 `02-evaluation/scorer.py`，参赛者不需要
传入 adapter 或 scorer 命令。`--workspace-source`、`--scorer-command` 和
`--agent-command` 仅作为开发调试 override，并通过
`VERIFORGE_SCORER_RESULT` 指向当前 roll 的结果路径。

固定 profile 的统一参数不会直接冒充厂商参数。runner 内置 provider
adapter，并将转换后的请求片段放在 `VERIFORGE_NATIVE_PARAMETERS_JSON`
和 `VERIFORGE_PROVIDER_REQUEST_JSON`：OpenAI Responses 使用
`reasoning.effort`/`max_output_tokens`，Anthropic 使用
`output_config.effort`/`max_tokens`，Kimi、Qwen、DeepSeek Chat 使用
`reasoning_effort`/`max_tokens`。任务 adapter 必须使用转换后的字段。

生成包中的 `03-runner/isolation-manifest.yaml` 声明 fixture、只读和可写
路径。runner 会在每个 roll 前后校验声明的 fixture 和 task spec；发现
Agent 修改契约或输入 fixture 时，该 roll 会在评分前失败。

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
