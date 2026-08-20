---
name: eng-veriforge
description: >-
  Use when designing, formalizing, or executing a local Agent benchmark task from
  an idea or workflow, including generating the task statement, output-file
  contract, reference answers, graders, fixtures, harness allowlists, and a
  participant-selected model rollout, fixed hyperparameter profiles, and
  multi-roll evidence generation.
---

# VeriForge — 可验证任务工坊

## Purpose

VeriForge is a single skill for turning either a rough task idea or an existing
Agent workflow into a local, verifiable benchmark package. It produces three
artifacts in one task directory:

1. **Task definition** — statement, objective, initial state, constraints,
   expected output files, output schemas, success criteria, and limitations.
2. **Evaluation assets** — reference answers, fixtures, rubric, deterministic
   validators, optional judge configuration, and a scorer.
3. **Task runner** — harness setup, Agent invocation, participant selection of
   one organizer-approved model, runtime API-key input, fixed hyperparameter
   configuration, scoring, and `N`-roll evidence generation.

This skill is local-first. It does not upload ZIPs, create Harbor jobs, or run
cloud tasks in the MVP.

## Task states

Use one of these states in `task.yaml`:

- `concept`: the task is an idea; generate a reviewable package skeleton, but do
  not claim reproducibility or produce publishable scores.
- `prototype`: the task, fixtures, or grader exist but are not fully validated;
  local runs are experimental only.
- `verified`: task, evaluation assets, runner, and output contract have been
  exercised together; results are reproducible enough to report.
- `blocked`: a required dependency, permission, fixture, de-identification
  requirement, or harness isolation guarantee is unavailable.

Never upgrade a task to `verified` merely because the prompt looks reasonable.

## Workflow

### 1. Model the task

Collect or infer, then confirm with the user:

- title, business context, objective, and non-goals;
- Agent inputs and initial state;
- files, directories, or systems the Agent may change;
- required output files, paths, formats, and schemas;
- success, partial-success, and failure criteria;
- reference-answer source and acceptable answer variance;
- external side effects such as send, delete, publish, write, payment, or approval;
- required MCPs, CLIs, Skills, directories, environment variables, and network.

If the activity specifies an approved model list, collect the canonical API
model ID, provider, endpoint, runtime credential variable, and fixed
parameters for every model. Keep display names separate from API IDs. Do not
infer provider-specific IDs or parameters from a marketing name. Participants
may choose one model per run, but the model matrix and that model's parameters
are organizer-provided and immutable.

If the user only has an idea, propose one or more measurable task versions and
identify the missing evidence. It is valid to create a `concept` package before
the task has ever been run manually.

### 2. Check dependencies and build the allowlist

Create `03-runner/dependency-manifest.yaml`, then perform read-only checks:

- MCP registration or command availability;
- CLI availability, using only harmless `--version` or `--help` checks when
  useful;
- file and directory existence and workspace containment;
- environment-variable presence by name only, never by value;
- declared network requirements; do not probe external systems unless the user
  explicitly requests a connectivity test.

Classify each dependency as `ready`, `missing`, or `unverified`.

Present the proposed allowlist and ask the user to confirm:

- MCPs visible to the harness session;
- CLIs exposed to the Agent;
- read-only and read-write mounts;
- Skills visible to the Agent;
- runtime secret variable names;
- permitted network domains.

Only confirmed, available, and workspace-safe dependencies may enter
`harness.allowlist.yaml`. Missing, unconfirmed, or over-broad capabilities stay
out of the harness.

If the underlying harness cannot enforce the allowlist, set the task state to
`blocked`; do not claim isolation.

### 3. Generate the three artifacts

Use this layout:

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
│   ├── models.yaml
│   ├── harness.allowlist.yaml
│   └── dependency-manifest.yaml
├── results/
└── evidence/
```

When this repository's `templates/runner/run_task.py` is available, use it as
the baseline for model selection and fixed-profile resolution while preserving
its allowlist behavior.
Add the task-specific harness adapter around that baseline instead of
reimplementing selection in the prompt.

Rules:

- Keep `task.yaml` as the task's source of truth.
- Keep task IDs and output paths identical across task, rubric, scorer, and
  runner.
- Prefer deterministic validation; use human/model judging only when the
  rubric defines its input, version, variance policy, and fallback behavior.
- Store only de-identified fixtures and evidence.
- Reject secrets, cookies, passwords, production data, absolute personal paths,
  and broad home-directory mounts.
- Default network to deny and external side effects to forbidden.
- Use `veriforge-model-matrix/v2` when the task exposes a participant model
  choice. The matrix must contain only organizer-approved models.
- Set `selection.mode` to `participant_selects_one`, disable participant
  profile/custom-parameter choices, and define the same fixed profile ID for
  every model. The profile is selected automatically after the participant
  chooses a model.

### 4. Generate and run the task script

The generated runner must execute this sequence:

```text
preflight dependencies and runtime secrets
  -> construct the allowlisted harness session
  -> invoke the Agent with fixed model/hyperparameters
  -> validate output files
  -> run the scorer
  -> persist per-roll logs, scores, and run manifests
  -> aggregate model × N-roll results
```

The participant runner must support:

```bash
python 03-runner/run_task.py --preflight
python 03-runner/run_task.py --model MODEL_ID --rolls N
python 03-runner/run_task.py --interactive --rolls N
```

If a task exposes an adapter command, both of these forms are valid and must
behave identically:

```bash
python 03-runner/run_task.py --model MODEL_ID --rolls 1 --agent-command ./03-runner/agent.sh
python 03-runner/run_task.py --model MODEL_ID --rolls 1 --agent-command -- ./03-runner/agent.sh
```

The runner must print a non-secret lifecycle message when each roll starts and
finishes, report a bounded failure diagnostic, and enforce the profile's
`timeout_seconds`. Redirecting all child output and leaving the participant
with no progress signal is not an acceptable runner UX.
Task-specific adapters must propagate a non-zero Agent exit code and retain a
bounded, secret-redacted stderr diagnostic; they must not redirect failures to
`/dev/null`.

When `--model` is omitted, prompt from the allowlisted model entries in
`models.yaml`. After model selection, if its credential environment variable
is absent, prompt for the API key using hidden input. Do not display, persist,
or log the key. Select the fixed profile automatically; never ask the
participant to choose a profile. Reject unknown model IDs and arbitrary
command-line hyperparameter overrides. A run selects exactly one model and a
roll count within `roll_policy`.

The model matrix and hyperparameters are fixed in the generated task version.
Do not let conversational instructions silently override them. Changes require
an explicit experiment configuration and an auditable task/version update.

When a required API key is absent, ask for its value only at runtime using
hidden input, or report the missing variable in non-interactive mode. Inject it
into the child process memory only. Never write it to task files, logs,
manifests, results, shell history, or error messages. The credential variable
name belongs in `models.yaml`; its value never does.

### Participant workflow

The participant uses the Skill to author a task package, then runs the same
participant runner while iterating on the task:

```text
task idea -> task/evaluation package -> choose one model -> enter its API key
  -> choose N rolls -> invoke Agent -> validate and score each roll
  -> inspect failure evidence -> refine the benchmark
```

There are no debug and official runner modes. A participant may repeat the
same command with different allowed models to compare failure patterns. The
selected model, fixed parameters, roll count, and scores must be recorded in
the run manifest. The task status (`concept`, `prototype`, `verified`, or
`blocked`) describes validation state; it is not a runner mode.

## Safety and isolation

- Do not install, log in, authorize, or repair MCPs, CLIs, or third-party
  accounts automatically.
- Do not expose global MCP/Skill configuration, the user's home directory, or
  unrestricted network to a harness session.
- Use read-only mounts for fixtures and a per-run output directory.
- Block tasks that require real destructive or irreversible side effects unless
  the user provides an isolated test account and an explicit rollback plan.
- Do not start an Agent run if preflight reports a required `missing` dependency.
- Do not emit `official_result: true` for `concept`, `prototype`, or `blocked`
  tasks.
- Do not expose an unapproved provider, model, profile, or hyperparameter
  override through the participant-facing runner.
- Record runner dependencies such as PyYAML in
  `03-runner/dependency-manifest.yaml` and preflight them before execution.
- Test the credential prompt, both `--agent-command` forms, roll lifecycle
  messages, timeout handling, and a missing-command failure before handoff.

## Self-check before handoff

Verify:

- the task statement and output-file standard are unambiguous;
- the reference answer and fixtures are readable;
- every rubric dimension has evidence or is explicitly human-judged;
- scorer paths match the output paths;
- model IDs, hyperparameters, roll limits, concurrency, retries, and result
  locations are recorded;
- interactive model selection and allowlist rejection paths work;
- the fixed profile is selected automatically and recorded with the chosen
  model;
- the allowlist contains only confirmed, available capabilities;
- no secret, real customer data, absolute personal path, or broad mount exists;
- the task state accurately reflects the validation level.

Report the task objective, state, three artifact paths, dependency status,
allowlist confirmation status, remaining user preparation, and the command to
run the generated task.
