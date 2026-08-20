# VeriForge Task Contract v1

## `task.yaml`

```yaml
schema_version: "veriforge-task/v1"
task_id: "example-task-v1"
title: "A measurable Agent task"
status: verified
execution_mode: local_only
objective: "What the Agent must accomplish"
agent_instructions: "Task instructions shown to the Agent"
initial_state:
  read_only_paths: []
  mutable_paths: []
required_output:
  path: "outputs/result.json"
  format: json
  schema:
    required_fields: []
constraints:
  side_effects: forbidden
  network: deny_by_default
  forbidden_actions: []
acceptance:
  minimum_score: 75
  fatal_rules: []
known_limitations: []
```

`status: verified` is a fixed package label. It is not inferred from a model
score, changed during rollout, or edited by participants. `acceptance.minimum_score`
is only the pass threshold for one Agent submission; the score and execution
status are the runtime results. Authors run the deterministic smoke checks and
dependency/fixture/allowlist checks before handoff, but those checks do not
introduce another task status.

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
    rule: "No forbidden side effect is allowed."
pass_policy:
  min_score: 75
  required_dimensions: [task_correctness, safety]
```

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

Every participant package also includes `03-runner/harnesses.yaml` with exactly
two entries: `cc` (the `claude` executable and Anthropic protocol) and `codex`
(the `codex` executable and OpenAI Responses protocol). Credentials and base
URLs come from the selected model entry. CC is restricted to file/search tools,
and the runner creates a separate config and home directory for each roll.

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
    display_name: "Human readable name"
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

`display_name` is participant-facing text; `model_name` is the canonical ID
sent to the provider. A participant selects exactly one model per run. The
runner automatically uses `selection.fixed_profile` for that model; profiles
and custom parameters are not participant choices. A missing
`credential_env` value may be entered at runtime using hidden input and must
never be persisted.

## Participant entry point

Every generated package exposes one primary participant command from the
package root:

```bash
./03-runner/run_benchmark.sh
```

The wrapper changes to the package root, explains the flow, prompts first for
`cc` (Claude Code) or `codex`, then for one compatible canonical model, requests
one selected-model API key with hidden input, and runs the default rollout count. An
optional first argument changes the
rollout count. Direct Python flags are advanced/CI interfaces. If no explicit
result directory is supplied, the runner creates
`results/<model-id>-<high-resolution-UTC-timestamp>/` so repeated runs cannot
reuse a prior roll workspace.

For a fixed activity, `models.yaml` may include:

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

Every participant-facing package MUST include this block with
`fixed_model_count: 5`. The runner requires the exact canonical ID set
`{kimi-k3, deepseek-v4-pro, qwen3.8-max, claude-opus-5, gpt-5.6-sol}`; missing,
extra, or substituted IDs are invalid. It also rejects `REPLACE_WITH_*` values,
provider/adapter/endpoint/credential mappings that differ from
`examples/activity-models.yaml`, and participant profile or parameter
overrides. The activity credential mode is fixed to `per_selected_model`.
Claude and GPT use Wodex, Qwen uses the organizer-provided Aliyun MaaS endpoint,
Kimi uses Moonshot, and DeepSeek uses its official endpoint. The lock is a task-package policy; a competition
distributor should also publish an artifact checksum or otherwise prevent
participants from editing `models.yaml` after distribution.

The activity matrix uses one immutable `default` profile per model. The fixed
activity baseline is:

| Model | Fixed reasoning | Fixed output limit |
| --- | --- | --- |
| `kimi-k3` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `deepseek-v4-pro` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `qwen3.8-max` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `claude-opus-5` | `reasoning_effort: max` | `max_output_tokens: 32768` |
| `gpt-5.6-sol` | `reasoning_effort: max` | `max_output_tokens: 32768` |

`reasoning_effort: max` is the normalized VeriForge control. Each provider
adapter maps it to that provider's highest supported reasoning/thinking mode.
Do not expose provider-specific budget, temperature, top-p, or alternate
profiles to participants. This five-model baseline is the only participant
matrix until the organizer explicitly requests a skill revision after
evaluating a standard test set.

## \`isolation-manifest.yaml\`

Every generated package should include this file under \`03-runner/\`:

\`\`\`yaml
schema_version: "veriforge-isolation-manifest/v1"
task_id: "example-task-v1"
fixture_paths:
  - "02-evaluation/fixtures"
read_only_paths:
  - "02-evaluation/fixtures"
  - "01-task/task.yaml"
mutable_paths:
  - "outputs"
\`\`\`

All paths are relative to the staged Agent workspace. The runner creates a
fresh workspace for every roll, keeps scorer/reference/rubric/validator assets
outside that workspace, and executes scorer from a trusted read-only evaluation
copy. It hashes every fixture, read-only path, task spec, scorer, allowlist,
dependency manifest, model catalog, and isolation manifest before and after the
Agent runs. Any change to a protected control or any workspace path outside
\`mutable_paths\` fails the roll before scoring. \`read_only_paths\` and
\`mutable_paths\` are enforced by the local fallback with the before/after
integrity checks; a sandbox backend may additionally enforce them as mounts.

## Run manifest

Each roll must record task ID/version, fixed `benchmark_status: verified`,
selected model ID, provider and adapter, fixed canonical parameters, the
provider-native parameter fragment, roll count and roll number, start/end
timestamps, runner version, harness allowlist hash, dependency manifest hash,
model and isolation manifest hashes, task spec hash, scorer hash, scorer
version, execution status, score, and output paths. It must also record unique
paths for the roll workspace, output directory, stdout/stderr logs, and scorer
result. It must not contain secret values or credential environment variable
values.

The canonical profile is translated before the Agent adapter runs. The built-in
provider mappings are:

| Adapter | Native reasoning field | Native output-limit field |
| --- | --- | --- |
| `openai_responses` | `reasoning.effort` | `max_output_tokens` |
| `anthropic_messages` | `output_config.effort` | `max_tokens` |
| `openai_chat` | `reasoning_effort` | `max_tokens` |

The translated fragment is exposed as `VERIFORGE_NATIVE_PARAMETERS_JSON` and
the complete non-secret request metadata as
`VERIFORGE_PROVIDER_REQUEST_JSON`. An adapter must not forward
`reasoning_effort` and `max_output_tokens` directly when the selected provider
uses another field shape.

## Adapter and output-contract handoff

`task.yaml` is the source of truth for the Agent instructions and required
output shape. The participant runner exposes its absolute path as
`VERIFORGE_TASK_SPEC`. A task-specific adapter must stage a copy with write
permission removed inside the isolated Agent workspace and explicitly tell the
Agent to read it. If staging changes paths, the adapter must document the
mapping (for example, `02-evaluation/fixtures` to `fixtures/`).

The adapter prompt must repeat the exact required output filenames, JSON object
keys, allowed enum values, required Markdown headings, evidence reference
shape, and fatal safety constraints. Filenames alone are insufficient because
an Agent can produce a plausible but validator-incompatible schema. The
adapter must reject missing outputs or non-zero Agent exits before scoring.

The default generated adapters are `03-runner/cc_agent.sh` and
`03-runner/codex_agent.sh`. They receive one provider-neutral runtime key, stage
the complete contract in the isolated workspace, invoke only the selected CLI,
and write declared outputs under `VERIFORGE_OUTPUT_DIR`. CC uses the selected
model's approved Anthropic-compatible variables; Codex uses a per-roll
Responses-compatible provider config. The
participant runner invokes the selected harness and then
`02-evaluation/scorer.py`. `provider_agent.py`, if present, is developer-only
legacy code and is never the participant default.

Every string field that the scorer compares exactly must have its closed
codebook declared in `task.yaml` and repeated in the adapter prompt. Exact-match
values must not exist only in `reference_answer/`, because the Agent would be
asked to guess a hidden evaluator convention.

Before a task is handed off, run two deterministic smoke checks: the reference
answer must pass the scorer, and a deliberately malformed output using missing
or alternate keys must fail the schema validator with a bounded diagnostic.
