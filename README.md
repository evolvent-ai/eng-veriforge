# eng-veriforge

VeriForge（可验证任务工坊）是一个标准 Agent Skill，用于把任务构思或既有工作流制作成可验证的本地 Agent Benchmark。

## 三类产物

1. 题目定义：题干、初始状态、约束和输出文件标准。
2. 评测资产：标答、fixture、rubric、验证器和评分器。
3. 执行脚本：隔离 harness、Agent 调用、固定模型/超参、评分和 `N` 次 roll 聚合。

## 当前边界

- 单一 Skill，不拆分 Skill2。
- 支持从 `concept` 开始，不要求任务已经手工跑通。
- 运行前只读检查 MCP、CLI、路径和环境变量。
- 只有用户确认且检查通过的能力才进入 harness allowlist。
- API Key 只在运行时注入，不写入任务包或日志。
- 当前版本本地优先，不包含云端 ZIP 上传、Harbor 调度或凭据托管。

详细契约见 `references/task-contract.md`。

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
环境变量读取，绝不会写入配置、manifest 或错误日志。

参赛者只能选择 `models.yaml` 中声明的模型，不能通过命令行临时覆盖
temperature、top_p、max token 或 reasoning 参数。选择模型后，若对应的
API Key 环境变量不存在，runner 会用隐藏输入提示输入 Key；Key 只注入
当前 rollout 进程，不会写入文件、日志或 manifest。

runner 模板使用 Python 3 和 PyYAML 读取 `models.yaml`；生成任务时应在
`03-runner/dependency-manifest.yaml` 中记录该依赖并进行预检。
