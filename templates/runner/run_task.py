#!/usr/bin/env python3
"""Participant runner for one allowlisted model and N rollout attempts.

Generated task runners should replace ``invoke_agent`` with their harness
adapter, or pass an agent command with ``--agent-command``.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import re
from datetime import datetime, timezone

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by preflight users
    raise SystemExit("PyYAML is required to read models.yaml") from exc


RUNNER_VERSION = "veriforge-runner/v1.5"
SECRET_VALUE_PATTERN = re.compile(r"(?i)(api[_ -]?key|password|secret|cookie|access[_ -]?token)\s*[:=]\s*[^\s,;]+")
PLACEHOLDER_MARKER = "REPLACE_WITH_"
FIXED_PARAMETERS = {"reasoning_effort": "max", "max_output_tokens": 32768}
CANONICAL_MODELS = {
    "kimi-k3": {
        "provider": "moonshot",
        "adapter": "openai_chat",
        "model_name": "kimi-k3",
        "endpoint": "chat_completions",
        "base_url": "https://api.moonshot.ai/v1",
        "credential_env": "MOONSHOT_API_KEY",
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "adapter": "openai_responses",
        "model_name": "deepseek-v4-pro",
        "endpoint": "responses",
        "base_url": "https://api.deepseek.com",
        "credential_env": "DEEPSEEK_API_KEY",
    },
    "qwen3.8-max": {
        "provider": "qwen",
        "adapter": "openai_chat",
        "model_name": "qwen3.8-max",
        "endpoint": "chat_completions",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "credential_env": "DASHSCOPE_API_KEY",
    },
    "claude-opus-5": {
        "provider": "anthropic",
        "adapter": "anthropic_messages",
        "model_name": "claude-opus-5",
        "endpoint": "messages",
        "base_url": "https://api.anthropic.com",
        "credential_env": "ANTHROPIC_API_KEY",
    },
    "gpt-5.6-sol": {
        "provider": "openai",
        "adapter": "openai_responses",
        "model_name": "gpt-5.6-sol",
        "endpoint": "responses",
        "base_url": "https://api.openai.com/v1",
        "credential_env": "OPENAI_API_KEY",
    },
}
CANONICAL_MODEL_IDS = frozenset(CANONICAL_MODELS)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_catalog(path: Path) -> dict:
    try:
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"models file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(catalog, dict):
        raise ValueError("models.yaml must contain a mapping")
    if catalog.get("schema_version") != "veriforge-model-matrix/v2":
        raise ValueError("models.yaml must use veriforge-model-matrix/v2")

    models = catalog.get("models")
    if not isinstance(models, list):
        raise ValueError("models.yaml must declare a models list")
    if len(models) != len(CANONICAL_MODELS):
        raise ValueError("models.yaml must contain exactly the five canonical models")

    selection = catalog.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("models.yaml needs a selection mapping")
    if selection.get("mode") != "participant_selects_one":
        raise ValueError("selection.mode must be participant_selects_one")
    if selection.get("allow_participant_model_choice") is not True:
        raise ValueError("participant model choice must be enabled")
    if selection.get("allow_participant_profile_choice") is not False:
        raise ValueError("participant profile choice must be disabled")
    if selection.get("allow_custom_parameters") is not False:
        raise ValueError("custom parameters must be disabled")
    if selection.get("max_models_per_run", 1) != 1:
        raise ValueError("a participant run may select exactly one model")
    fixed_profile = selection.get("fixed_profile", "default")
    if fixed_profile != "default":
        raise ValueError("selection.fixed_profile must be default")

    organizer_controls = catalog.get("organizer_controls")
    if not isinstance(organizer_controls, dict):
        raise ValueError("organizer_controls is required for the canonical matrix")
    if organizer_controls.get("model_matrix_locked") is not True:
        raise ValueError("organizer_controls.model_matrix_locked must be true")
    if organizer_controls.get("participant_model_choice_only") is not True:
        raise ValueError("organizer_controls.participant_model_choice_only must be true")
    if organizer_controls.get("fixed_profile_only") is not True:
        raise ValueError("organizer_controls.fixed_profile_only must be true")
    if organizer_controls.get("fixed_model_count") != len(CANONICAL_MODELS):
        raise ValueError("organizer_controls.fixed_model_count must be 5")
    if organizer_controls.get("credential_mode") != "per_selected_model":
        raise ValueError("organizer_controls.credential_mode must be per_selected_model")
    approved_ids = organizer_controls.get("approved_model_ids")
    if not isinstance(approved_ids, list) or set(approved_ids) != CANONICAL_MODEL_IDS or len(approved_ids) != len(CANONICAL_MODELS):
        raise ValueError("organizer_controls.approved_model_ids must be the five canonical IDs")

    seen_models: set[str] = set()
    seen_credentials: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("each model must be a mapping")
        model_id = model.get("id")
        if model_id not in CANONICAL_MODEL_IDS:
            raise ValueError(f"model ID is not in the canonical matrix: {model_id}")
        if model_id in seen_models:
            raise ValueError(f"duplicate model id: {model_id}")
        seen_models.add(model_id)
        canonical = CANONICAL_MODELS[model_id]
        for field, expected in canonical.items():
            value = model.get(field)
            if value != expected:
                raise ValueError(f"model {model_id}.{field} must equal the canonical matrix value")
            if isinstance(value, str) and PLACEHOLDER_MARKER in value:
                raise ValueError(f"model {model_id} still contains an organizer placeholder in {field}")
        credential_env = canonical["credential_env"]
        if credential_env in seen_credentials:
            raise ValueError(f"credential_env must be unique: {credential_env}")
        seen_credentials.add(credential_env)
        profiles = model.get("profiles")
        if not isinstance(profiles, list) or len(profiles) != 1:
            raise ValueError(f"model {model_id} must declare exactly one profile")
        profile = profiles[0]
        if not isinstance(profile, dict) or profile.get("id") != "default":
            raise ValueError(f"model {model_id} must declare the sole default profile")
        if profile.get("parameters") != FIXED_PARAMETERS:
            raise ValueError(f"model {model_id} default profile must use the fixed parameters")
    if seen_models != CANONICAL_MODEL_IDS:
        raise ValueError("models.yaml must contain exactly the five canonical model IDs")

    return catalog


def catalog_hash(catalog: dict) -> str:
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_task_spec(path: Path) -> None:
    """Require the generated package's single fixed benchmark status."""
    try:
        task = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"task spec not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in task spec: {exc}") from exc
    if not isinstance(task, dict):
        raise ValueError("task spec must contain a mapping")
    if task.get("status") != "verified":
        raise ValueError("task spec must set status: verified")
    if "release" in task or "lifecycle" in task:
        raise ValueError("task spec must not contain release or lifecycle metadata")


def choose_index(items: list[tuple[str, str]], label: str) -> str:
    print(label)
    for index, (item_id, display_name) in enumerate(items, start=1):
        print(f"{index}. {display_name} ({item_id})")
    while True:
        try:
            answer = input("选择编号: ").strip()
            selected = items[int(answer) - 1]
        except (EOFError, ValueError, IndexError):
            print(f"请输入 1-{len(items)} 之间的编号。", file=sys.stderr)
            continue
        return selected[0]


def resolve_models(catalog: dict, args: argparse.Namespace) -> list[dict]:
    models = catalog["models"]
    by_id = {model["id"]: model for model in models}
    if args.preflight and not args.model and not args.interactive:
        return models
    if args.model:
        if args.model not in by_id:
            raise ValueError(f"model is not allowlisted: {args.model}")
        return [by_id[args.model]]

    if args.interactive or sys.stdin.isatty():
        choices = [(model["id"], model.get("display_name", model["id"])) for model in models]
        return [by_id[choose_index(choices, "请选择模型:")]]
    raise ValueError("provide --model or run in an interactive terminal")


def resolve_profile(catalog: dict, model: dict) -> dict:
    profiles = model["profiles"]
    by_id = {profile["id"]: profile for profile in profiles}
    fixed_profile = catalog["selection"].get("fixed_profile", "default")
    if fixed_profile not in by_id:
        raise ValueError(f"fixed profile is not declared for {model['id']}: {fixed_profile}")
    return by_id[fixed_profile]


def check_credentials(models: list[dict]) -> list[str]:
    missing = []
    for model in models:
        variable = model.get("credential_env")
        if variable and not os.environ.get(variable):
            missing.append(f"{model['id']}:{variable}")
    return missing


def build_environment(model: dict, profile: dict, credential: str | None, task_spec: Path) -> dict[str, str]:
    """Pass only the selected credential and VeriForge metadata to the child."""
    env = {"PATH": os.environ.get("PATH", "")}
    if os.environ.get("CODEX_BIN"):
        env["CODEX_BIN"] = os.environ["CODEX_BIN"]
    credential_env = model.get("credential_env")
    if credential_env and credential:
        env[credential_env] = credential
    env.update(
        {
            "VERIFORGE_MODEL_ID": model["id"],
            "VERIFORGE_MODEL_NAME": model["model_name"],
            "VERIFORGE_PROVIDER": str(model.get("provider", "")),
            "VERIFORGE_ADAPTER": str(model.get("adapter", "")),
            "VERIFORGE_ENDPOINT": str(model.get("endpoint", "")),
            "VERIFORGE_BASE_URL": str(model.get("base_url", "")),
            "VERIFORGE_CODEX_MODEL_PROVIDER": str(model.get("codex_model_provider", "")),
            "VERIFORGE_CODEX_BASE_URL": str(model.get("codex_base_url", "")),
            "VERIFORGE_TASK_SPEC": str(task_spec),
            "VERIFORGE_PROFILE_ID": profile["id"],
            "VERIFORGE_PARAMETERS_JSON": json.dumps(profile.get("parameters", {}), sort_keys=True),
        }
    )
    return env


def redact_diagnostic(text: str, credential: str | None) -> str:
    """Keep failure diagnostics useful without persisting or echoing a key."""
    if credential:
        text = text.replace(credential, "<redacted>")
    text = SECRET_VALUE_PATTERN.sub(lambda match: match.group(1) + "=<redacted>", text)
    return text.strip()[-2000:]


def run_roll(
    model: dict,
    profile: dict,
    credential: str | None,
    args: argparse.Namespace,
    index: int,
    config_digest: str,
    task_spec_digest: str,
) -> dict:
    started = utc_now()
    command = args.agent_command
    record = {
        "task_id": args.task_id,
        "model_id": model["id"],
        "model_name": model["model_name"],
        "profile_id": profile["id"],
        "parameters": profile.get("parameters", {}),
        "roll": index,
        "runner_version": RUNNER_VERSION,
        "models_hash": config_digest,
        "task_spec_hash": task_spec_digest,
        "benchmark_status": "verified",
        "started_at": started,
        "status": "dry_run" if args.dry_run else "pending",
    }
    if args.dry_run:
        record["ended_at"] = utc_now()
        return record
    if not command:
        raise ValueError("an agent command is required unless --dry-run is used")

    print(f"[veriforge] roll {index}/{args.rolls}: starting agent", flush=True)
    try:
        completed = subprocess.run(
            command,
            env=build_environment(model, profile, credential, args.task_spec),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=profile.get("timeout_seconds"),
        )
    except subprocess.TimeoutExpired:
        record["returncode"] = 124
        record["status"] = "failed"
        record["timeout_seconds"] = profile.get("timeout_seconds")
        record["ended_at"] = utc_now()
        print(
            f"[veriforge] roll {index}/{args.rolls}: agent timed out after {profile.get('timeout_seconds')}s",
            flush=True,
        )
        return record
    except OSError as exc:
        record["returncode"] = 127
        record["status"] = "failed"
        record["ended_at"] = utc_now()
        print(f"[veriforge] roll {index}/{args.rolls}: could not start agent: {exc}", flush=True)
        return record
    record["returncode"] = completed.returncode
    record["status"] = "passed" if completed.returncode == 0 else "failed"
    if completed.returncode != 0:
        for stream_name, stream in (("stdout", completed.stdout), ("stderr", completed.stderr)):
            diagnostic = redact_diagnostic(stream or "", credential)
            if diagnostic:
                record[f"agent_{stream_name}_tail"] = diagnostic
                print(f"[veriforge] agent {stream_name}: {diagnostic}", file=sys.stderr, flush=True)
        print(f"[veriforge] roll {index}/{args.rolls}: agent failed (exit {completed.returncode})", flush=True)
    else:
        print(f"[veriforge] roll {index}/{args.rolls}: agent finished", flush=True)
    record["ended_at"] = utc_now()
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one allowlisted VeriForge model for N rolls")
    parser.add_argument("--models-file", type=Path, default=Path("03-runner/models.yaml"))
    parser.add_argument("--task-id", default="unversioned-task")
    parser.add_argument("--model")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--rolls", type=int)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--task-spec", type=Path, default=Path("01-task/task.yaml"))
    parser.add_argument("--agent-command", nargs=argparse.REMAINDER)
    # Accept both ``--agent-command command`` and the conventional
    # ``--agent-command -- command`` form used in shell examples.
    argv = sys.argv[1:]
    if "--agent-command" in argv:
        command_index = argv.index("--agent-command")
        if command_index + 1 < len(argv) and argv[command_index + 1] == "--":
            argv.pop(command_index + 1)
    args = parser.parse_args(argv)
    if args.agent_command and args.agent_command[0] == "--":
        args.agent_command = args.agent_command[1:]
    return args


def main() -> int:
    args = parse_args()
    catalog = load_catalog(args.models_file)
    roll_policy = catalog.get("roll_policy", {})
    if args.rolls is None:
        args.rolls = roll_policy.get("default_rolls", 1)
    min_rolls = roll_policy.get("min_rolls", 1)
    max_rolls = roll_policy.get("max_rolls", args.rolls)
    if args.rolls < min_rolls or args.rolls > max_rolls:
        raise ValueError(f"--rolls must be between {min_rolls} and {max_rolls}")

    models = resolve_models(catalog, args)
    args.task_spec = args.task_spec.resolve()
    if not args.task_spec.is_file():
        if args.preflight:
            print(f"task spec not found: {args.task_spec}", file=sys.stderr)
            return 2
        raise ValueError(f"task spec not found: {args.task_spec}")
    validate_task_spec(args.task_spec)
    if args.preflight:
        # Credential mode is per_selected_model. A matrix-only preflight may
        # report all five variables, but only an explicitly selected model can
        # make the preflight fail for a missing credential.
        selected_for_check = bool(args.model or args.interactive)
        missing = check_credentials(models) if selected_for_check else []
        for model in models:
            variable = model.get("credential_env")
            state = "ready" if not variable or os.environ.get(variable) else "missing"
            print(f"{model['id']}: credential {state} ({variable or 'none'})")
        if missing and not args.dry_run:
            return 2
        return 0

    model = models[0]
    credential_env = model.get("credential_env")
    credential = os.environ.get(credential_env) if credential_env else None
    if not credential and not args.dry_run:
        if not credential_env:
            raise ValueError(f"model {model['id']} has no credential_env")
        if not sys.stdin.isatty():
            raise ValueError(f"missing runtime credential: {credential_env}")
        credential = getpass.getpass(
            f"请输入 {model.get('display_name', model['id'])} 的 API Key ({credential_env}): "
        )
        if not credential:
            raise ValueError("API Key cannot be empty")

    digest = catalog_hash(catalog)
    task_spec_digest = hashlib.sha256(args.task_spec.read_bytes()).hexdigest()
    records = []
    profile = resolve_profile(catalog, model)
    for roll in range(1, args.rolls + 1):
        records.append(
            run_roll(
                model,
                profile,
                credential,
                args,
                roll,
                digest,
                task_spec_digest,
            )
        )

    args.results_dir.mkdir(parents=True, exist_ok=True)
    output = args.results_dir / "run-manifest.json"
    output.write_text(
        json.dumps(
            {
                "task_id": args.task_id,
                "benchmark_status": "verified",
                "runs": records,
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0 if all(record["status"] in {"dry_run", "passed"} for record in records) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
