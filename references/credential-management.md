# Credential management

## Contract

Codeup access uses one runtime-only secret:

```text
Name: CODEUP_PAT
Purpose: Codeup API/Git access for an explicitly approved task
Scope: least privilege, limited to the required project/repository actions
Storage: approved secret manager or GitHub Actions repository/environment secret
Exposure: child process memory only
```

The PAT value must never appear in:

- Git history, source files, task fixtures, YAML, JSON, or `.env` files;
- command-line arguments, Git remote URLs, shell history, logs, artifacts, or
  benchmark result manifests;
- chat messages, issue comments, pull requests, or CI output.

## Local execution

Before a local run, the user or approved secret provider injects the variable:

```bash
export CODEUP_PAT=... # inject through the approved secret manager; do not paste into task files
python 03-runner/run_task.py --preflight
```

The runner may print only a redacted state such as `CODEUP_PAT: present` or
`CODEUP_PAT: missing`. It must not test the token by sending a request unless
the task explicitly enables a read-only connectivity check.

## GitHub Actions

For CI, create a repository or environment secret named `CODEUP_PAT` through the
organization's approved secret-management process. Workflow usage must pass it
through the environment, not the command line:

```yaml
jobs:
  benchmark:
    runs-on: ubuntu-latest
    environment: codeup
    steps:
      - uses: actions/checkout@v4
      - name: Run VeriForge task
        env:
          CODEUP_PAT: ${{ secrets.CODEUP_PAT }}
        run: python 03-runner/run_task.py --preflight
```

Do not add a placeholder secret, echo the value, or use `set -x`. Prefer a
protected environment with reviewers for tasks that can mutate Codeup. Read-only
Codeup tasks should use a read-only PAT with the smallest possible scope.

## Rotation and incident response

- Rotate the PAT in the approved secret manager, then update the repository or
  environment secret in the same maintenance window.
- Revoke the old PAT after consumers have switched.
- If exposure is suspected, revoke immediately, inspect CI logs and Git history,
  and create a replacement; do not attempt to hide the old value with a new
  commit.
- The skill must fail closed when the secret is missing or the task allowlist
  does not explicitly include `CODEUP_PAT`.
