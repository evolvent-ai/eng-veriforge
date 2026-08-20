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
from datetime import datetime, timezone

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by preflight users
    raise SystemExit("PyYAML is required to read models.yaml") from exc


RUNNER_VERSION = "veriforge-runner/v1"


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
    if not isinstance(models, list) or not models:
        raise ValueError("models.yaml must declare at least one model")

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

    seen_models: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("each model must be a mapping")
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("each model needs a non-empty id")
        if model_id in seen_models:
            raise ValueError(f"duplicate model id: {model_id}")
        seen_models.add(model_id)
        if not model.get("model_name"):
            raise ValueError(f"model {model_id} needs a canonical model_name")
        profiles = model.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            raise ValueError(f"model {model_id} needs at least one profile")
        seen_profiles: set[str] = set()
        for profile in profiles:
            if not isinstance(profile, dict) or not profile.get("id"):
                raise ValueError(f"model {model_id} contains an invalid profile")
            profile_id = profile["id"]
            if profile_id in seen_profiles:
                raise ValueError(f"duplicate profile {profile_id} for model {model_id}")
            seen_profiles.add(profile_id)
            if not isinstance(profile.get("parameters", {}), dict):
                raise ValueError(f"profile {profile_id} for {model_id} needs a parameter mapping")
        if fixed_profile not in seen_profiles:
            raise ValueError(f"model {model_id} has no fixed profile {fixed_profile}")

    return catalog


def catalog_hash(catalog: dict) -> str:
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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


def build_environment(model: dict, profile: dict, credential: str | None) -> dict[str, str]:
    """Pass only the selected credential and VeriForge metadata to the child."""
    env = {"PATH": os.environ.get("PATH", "")}
    credential_env = model.get("credential_env")
    if credential_env and credential:
        env[credential_env] = credential
    env.update(
        {
            "VERIFORGE_MODEL_ID": model["id"],
            "VERIFORGE_MODEL_NAME": model["model_name"],
            "VERIFORGE_PROVIDER": str(model.get("provider", "")),
            "VERIFORGE_PROFILE_ID": profile["id"],
            "VERIFORGE_PARAMETERS_JSON": json.dumps(profile.get("parameters", {}), sort_keys=True),
        }
    )
    return env


def run_roll(
    model: dict,
    profile: dict,
    credential: str | None,
    args: argparse.Namespace,
    index: int,
    config_digest: str,
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
        "started_at": started,
        "status": "dry_run" if args.dry_run else "pending",
    }
    if args.dry_run:
        record["ended_at"] = utc_now()
        return record
    if not command:
        raise ValueError("an agent command is required unless --dry-run is used")

    completed = subprocess.run(command, env=build_environment(model, profile, credential), check=False)
    record["returncode"] = completed.returncode
    record["status"] = "passed" if completed.returncode == 0 else "failed"
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
    parser.add_argument("--agent-command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
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
    if args.preflight:
        missing = check_credentials(models)
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
    records = []
    profile = resolve_profile(catalog, model)
    for roll in range(1, args.rolls + 1):
        records.append(run_roll(model, profile, credential, args, roll, digest))

    args.results_dir.mkdir(parents=True, exist_ok=True)
    output = args.results_dir / "run-manifest.json"
    output.write_text(json.dumps({"runs": records}, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(output)
    return 0 if all(record["status"] in {"dry_run", "passed"} for record in records) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
