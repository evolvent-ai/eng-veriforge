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

## Run manifest

Each roll must record task ID/version, selected model ID, fixed profile ID,
fixed parameters, roll count and roll number,
start/end timestamps, runner version, harness allowlist hash,
scorer version, status, score, and output paths. It must not contain secret
values or credential environment variable values.
