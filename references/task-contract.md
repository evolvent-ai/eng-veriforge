# VeriForge Task Contract v1

## `task.yaml`

```yaml
schema_version: "veriforge-task/v1"
task_id: "example-task-v1"
title: "A measurable Agent task"
status: concept # concept | prototype | verified | blocked
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
    model_name: "provider/model-id"
    endpoint: "responses"
    credential_env: "PROVIDER_API_KEY"
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

For a fixed activity, `models.yaml` may include:

```yaml
organizer_controls:
  model_matrix_locked: true
  participant_model_choice_only: true
  fixed_model_count: 5
  credential_mode: per_selected_model # or single_runtime_key
```

When this block is present, the runner requires exactly the declared number of
models, rejects `REPLACE_WITH_*` values, and rejects participant profile or
parameter overrides. `single_runtime_key` additionally requires a confirmed
`organizer_controls.credential_env` and the same `credential_env` on every
model. The lock is a task-package policy; a competition distributor should
also publish an artifact checksum or otherwise prevent participants from
editing `models.yaml` after distribution.

## Run manifest

Each roll must record task ID/version, selected model ID, fixed profile ID,
fixed parameters, roll count and roll number,
start/end timestamps, runner version, harness allowlist hash,
task spec hash, scorer version, status, score, and output paths. It must not
contain secret values or credential environment variable values.

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

Every string field that the scorer compares exactly must have its closed
codebook declared in `task.yaml` and repeated in the adapter prompt. Exact-match
values must not exist only in `reference_answer/`, because the Agent would be
asked to guess a hidden evaluator convention.

Before a task is handed off, run two deterministic smoke checks: the reference
answer must pass the scorer, and a deliberately malformed output using missing
or alternate keys must fail the schema validator with a bounded diagnostic.
