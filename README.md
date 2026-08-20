# eng-veriforge

VeriForge（可验证任务工坊）是一个标准 Agent Skill，用于把任务构思或既有工作流制作成可验证的本地 Agent Benchmark。

## 三类产物

1. 题目定义：题干、初始状态、约束和输出文件标准。
2. 评测资产：标答、fixture、rubric、验证器和评分器。
3. 执行脚本：隔离 harness、Agent 调用、固定模型/超参、评分和 `N` 次 roll 聚合。

任务包的生命周期状态（`concept`、`prototype`、`verified`、`blocked`）与
模型分数是两个独立维度。状态只供主办方在制作阶段检查，不是参赛者要
流转的流程。活动发布前，skill 必须完成发布门禁，并交付
`status: verified` 的任务包；`minimum_score` 只判断某一次提交是否通过
评分策略，不参与发布状态判断。

## 当前边界

- 单一 Skill，不拆分 Skill2。
- 作者可以从私有 `concept` 包开始；活动分发包必须在发布前完成验证并为
  `verified`。
- 运行前只读检查 MCP、CLI、路径和环境变量。
- 只有用户确认且检查通过的能力才进入 harness allowlist。
- API Key 只在运行时注入，不写入任务包或日志。
- 当前版本本地优先，不包含云端 ZIP 上传、Harbor 调度或凭据托管。

详细契约见 `references/task-contract.md`。

## 活动预配置

如果这是一个面向参赛者的固定活动，主办方应在任务包生成前锁定一个
`models.yaml`：模型数量、canonical model ID、provider、endpoint、credential
变量名和固定 profile 都由主办方提供。可以使用 `organizer_controls` 声明
固定五模型矩阵；runner 会拒绝数量不符或仍含 `REPLACE_WITH_*` 的配置。

参赛者拿到任务包后不需要填写 provider、endpoint 或模型 ID，只能从这五个
已批准模型中选择一个，并在运行时输入所选模型的 API Key。若五个模型必须
共用一个 API Key，主办方需要让它们通过同一个已确认的 provider gateway，
并把所有 `credential_env` 固定为同一个变量；否则使用
`per_selected_model`，参赛者每次只输入当前所选模型对应的一个 Key。

默认能力基线建议只包含：

- Codex CLI adapter；
- Claude Code（CC）CLI adapter，具体命令名和版本由主办方确认；
- Python 3 和 PyYAML 作为 runner 依赖。

默认不启用 MCP、额外 Skill、浏览器、外部应用或网络。CC/Codex 是执行
adapter 的 CLI 能力，不是 `models.yaml` 中可以由参赛者自由添加的 provider。
只有完成 `--version`/`--help` 等只读检查并经主办方确认的 CLI，才能写入
`harness.allowlist.yaml`。

## 参赛者模型选择

活动可以在任务包的 `03-runner/models.yaml` 中声明允许的模型和固定超参。
参赛者每次选择一个模型和 rollout 次数，runner 自动使用该模型的固定
参数。使用 `veriforge-model-matrix/v2` 时，runner 支持交互式选择：

```bash
python 03-runner/run_task.py --interactive --rolls 3
```

也支持 CI 的显式调用：

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

runner 会把任务定义文件的绝对路径通过 `VERIFORGE_TASK_SPEC` 传给 adapter。
adapter 必须把它复制到隔离工作目录，要求 Agent 先读取该文件，并在 prompt
中明确列出 validator 要求的精确字段名、枚举、证据引用格式和 Markdown
标题。只列出输出文件名，或让 Agent 自行发明 JSON key，不符合 VeriForge
契约。分发前应让 reference answer 通过 scorer，并让一个缺少必填字段或
使用替代 key 的输出以确定性 schema 错误失败。

如果另一台机器上的 Codex 可执行文件不在默认路径，adapter 可以使用
`CODEX_BIN`；runner 只会显式转发这个已知的非敏感变量，不会透传完整用户
环境。

参赛者只能选择 `models.yaml` 中声明的模型，不能通过命令行临时覆盖
temperature、top_p、max token 或 reasoning 参数。选择模型后，若对应的
API Key 环境变量不存在，runner 会用隐藏输入提示输入 Key；Key 只注入
当前 rollout 进程，不会写入文件、日志或 manifest。

runner 模板使用 Python 3 和 PyYAML 读取 `models.yaml`；生成任务时应在
`03-runner/dependency-manifest.yaml` 中记录该依赖并进行预检。
