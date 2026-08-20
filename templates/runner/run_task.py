#!/usr/bin/env python3
"""Participant runner for one approved model and N isolated harness rollouts."""

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
import shutil
import signal
import stat
from datetime import datetime, timezone

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by preflight users
    raise SystemExit("PyYAML is required to read models.yaml") from exc


RUNNER_VERSION = "veriforge-runner/v2.0"
SECRET_VALUE_PATTERN = re.compile(r"(?i)(api[_ -]?key|password|secret|cookie|access[_ -]?token)\s*[:=]\s*[^\s,;]+")
PLACEHOLDER_MARKER = "REPLACE_WITH_"
FIXED_PARAMETERS = {"reasoning_effort": "max", "max_output_tokens": 32768}
ISOLATION_MANIFEST_VERSION = "veriforge-isolation-manifest/v1"
CANONICAL_MODELS = {
    "kimi-k3": {
        "provider": "moonshot",
        "adapter": "openai_chat",
        "model_name": "kimi-k3",
        "endpoint": "chat_completions",
        "base_url": "https://api.moonshot.cn/v1",
        "credential_env": "MOONSHOT_API_KEY",
        "supported_harnesses": ["codex"],
        "codex_model_provider": "moonshot",
        "codex_base_url": "https://api.moonshot.cn/v1",
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "adapter": "openai_chat",
        "model_name": "deepseek-v4-pro",
        "endpoint": "chat_completions",
        "base_url": "https://api.deepseek.com",
        "credential_env": "DEEPSEEK_API_KEY",
        "supported_harnesses": ["codex"],
        "codex_model_provider": "deepseek",
        "codex_base_url": "https://api.deepseek.com",
    },
    "qwen3.8-max": {
        "provider": "aliyun_maas",
        "adapter": "openai_chat",
        "model_name": "qwen3.8-max",
        "endpoint": "chat_completions",
        "base_url": "https://llm-fw3e7y0h6s1otsjx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "credential_env": "DASHSCOPE_API_KEY",
        "supported_harnesses": ["codex"],
        "codex_model_provider": "aliyun_maas",
        "codex_base_url": "https://llm-fw3e7y0h6s1otsjx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    },
    "claude-opus-5": {
        "provider": "wodex",
        "adapter": "anthropic_messages",
        "model_name": "claude-opus-5",
        "endpoint": "messages",
        "base_url": "https://api.wodex.ai",
        "credential_env": "WODEX_API_KEY",
        "supported_harnesses": ["cc"],
    },
    "gpt-5.6-sol": {
        "provider": "wodex",
        "adapter": "openai_responses",
        "model_name": "gpt-5.6-sol",
        "endpoint": "responses",
        "base_url": "https://api.wodex.ai/v1",
        "credential_env": "WODEX_API_KEY",
        "supported_harnesses": ["codex"],
        "codex_model_provider": "wodex",
        "codex_base_url": "https://api.wodex.ai/v1",
    },
}
CANONICAL_MODEL_IDS = frozenset(CANONICAL_MODELS)
CANONICAL_HARNESSES = {
    "cc": {
        "display_name": "Claude Code (CC)",
        "executable": "claude",
        "executable_env": "CLAUDE_BIN",
        "protocol": "anthropic",
    },
    "codex": {
        "display_name": "Codex CLI",
        "executable": "codex",
        "executable_env": "CODEX_BIN",
        "protocol": "openai_responses",
    },
}
CANONICAL_HARNESS_IDS = frozenset(CANONICAL_HARNESSES)


def _openai_responses_parameters(parameters: dict) -> dict:
    return {
        "reasoning": {"effort": parameters["reasoning_effort"]},
        "max_output_tokens": parameters["max_output_tokens"],
    }


def _anthropic_messages_parameters(parameters: dict) -> dict:
    return {
        "output_config": {"effort": parameters["reasoning_effort"]},
        "max_tokens": parameters["max_output_tokens"],
    }


def _openai_chat_parameters(parameters: dict) -> dict:
    return {
        "reasoning_effort": parameters["reasoning_effort"],
        "max_tokens": parameters["max_output_tokens"],
    }


PROVIDER_ADAPTERS = {
    "openai_responses": _openai_responses_parameters,
    "anthropic_messages": _anthropic_messages_parameters,
    "openai_chat": _openai_chat_parameters,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_results_dir(model_id: str) -> Path:
    """Create a collision-resistant result path for participant runs."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = Path("results") / f"{model_id}-{timestamp}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    return candidate


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
        supported_harnesses = model.get("supported_harnesses")
        if not isinstance(supported_harnesses, list) or not supported_harnesses:
            raise ValueError(f"model {model_id} must declare supported_harnesses")
        if not set(supported_harnesses).issubset(CANONICAL_HARNESS_IDS):
            raise ValueError(f"model {model_id} declares an unapproved harness")
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


def load_harness_catalog(path: Path) -> dict:
    """Validate the two organizer-approved participant harnesses."""
    try:
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"harnesses file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(catalog, dict) or catalog.get("schema_version") != "veriforge-harness-matrix/v1":
        raise ValueError("harnesses.yaml must use veriforge-harness-matrix/v1")
    if catalog.get("credential_mode") != "per_selected_model":
        raise ValueError("harnesses.yaml must use per_selected_model credentials")
    entries = catalog.get("harnesses")
    if not isinstance(entries, list) or len(entries) != len(CANONICAL_HARNESSES):
        raise ValueError("harnesses.yaml must contain exactly cc and codex")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") not in CANONICAL_HARNESS_IDS:
            raise ValueError("harnesses.yaml contains an unapproved harness")
        harness_id = entry["id"]
        if harness_id in seen:
            raise ValueError(f"duplicate harness id: {harness_id}")
        seen.add(harness_id)
        canonical = CANONICAL_HARNESSES[harness_id]
        for field, expected in canonical.items():
            if entry.get(field) != expected:
                raise ValueError(f"harness {harness_id}.{field} does not match the approved harness mapping")
    if seen != CANONICAL_HARNESS_IDS:
        raise ValueError("harnesses.yaml must contain exactly cc and codex")
    return catalog


def catalog_hash(catalog: dict) -> str:
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(root: Path) -> str:
    """Hash a fixture tree by relative names and file contents."""
    digest = hashlib.sha256()
    if root.is_file():
        digest.update(root.name.encode("utf-8"))
        digest.update(root.read_bytes())
        return digest.hexdigest()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_isolation_manifest(workspace: Path, task_id: str) -> dict:
    """Load generated isolation metadata, with a compatibility fallback."""
    path = workspace / "03-runner" / "isolation-manifest.yaml"
    if not path.is_file():
        return {
            "schema_version": ISOLATION_MANIFEST_VERSION,
            "task_id": task_id,
            "fixture_paths": ["02-evaluation/fixtures"],
            "read_only_paths": ["02-evaluation/fixtures", "01-task/task.yaml"],
            "mutable_paths": ["outputs"],
            "manifest_path": None,
        }
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in isolation manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != ISOLATION_MANIFEST_VERSION:
        raise ValueError(f"isolation manifest must use {ISOLATION_MANIFEST_VERSION}")
    if manifest.get("task_id") != task_id:
        raise ValueError("isolation manifest task_id does not match the task")
    for key in ("fixture_paths", "read_only_paths", "mutable_paths"):
        values = manifest.get(key, ["02-evaluation/fixtures"] if key == "fixture_paths" else [])
        if not isinstance(values, list) or not all(isinstance(path, str) for path in values):
            raise ValueError(f"isolation manifest {key} must be a list of relative paths")
        for relative in values:
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError(f"isolation manifest path escapes workspace: {relative}")
        manifest[key] = values
    fixture_paths = manifest["fixture_paths"]
    manifest["manifest_path"] = str(path)
    return manifest


def fixture_hashes(workspace: Path, manifest: dict) -> dict[str, str]:
    hashes = {}
    for relative in manifest.get("fixture_paths", []):
        path = workspace / relative
        if path.exists():
            hashes[relative] = tree_hash(path)
    return hashes


def resolve_agent_command(
    workspace: Path,
    explicit: list[str] | None,
    harness_id: str | None = None,
) -> list[str]:
    """Resolve the selected real harness; keep provider_agent as legacy fallback."""
    if explicit:
        return explicit
    if harness_id == "cc":
        candidates = (workspace / "03-runner" / "cc_agent.sh",)
    elif harness_id == "codex":
        candidates = (workspace / "03-runner" / "codex_agent.sh",)
    else:
        # Compatibility for developer tests and old packages. Participant runs
        # always resolve through an explicit harness selection in main().
        candidates = (
            workspace / "03-runner" / "provider_agent.py",
            workspace / "03-runner" / "agent_adapter.py",
            workspace / "03-runner" / "codex_agent.sh",
            workspace / "03-runner" / "cc_agent.sh",
            workspace / "03-runner" / "agent.sh",
        )
    for candidate in candidates:
        if candidate.is_file():
            if candidate.suffix == ".py":
                return [sys.executable, str(candidate)]
            return [str(candidate)]
    raise ValueError(
        f"selected harness adapter not found for {harness_id or 'legacy'}; "
        "expected 03-runner/cc_agent.sh or 03-runner/codex_agent.sh"
    )


def resolve_scorer_command(workspace: Path, output_dir: Path, explicit: list[str] | None) -> list[str]:
    """Use the generated deterministic scorer unless a developer override is supplied."""
    if explicit:
        return explicit
    scorer = workspace / "02-evaluation" / "scorer.py"
    if not scorer.is_file():
        raise ValueError("generated scorer not found: 02-evaluation/scorer.py")
    return [sys.executable, str(scorer), "--outputs", str(output_dir)]


def validate_generated_package(
    source: Path,
    explicit_agent: list[str] | None,
    explicit_scorer: list[str] | None,
    harness_id: str | None = None,
) -> None:
    """Fail preflight before a participant enters a key if generated tools are missing."""
    resolve_agent_command(source, explicit_agent, harness_id)
    if explicit_scorer is None and not (source / "02-evaluation" / "scorer.py").is_file():
        raise ValueError("generated scorer not found: 02-evaluation/scorer.py")


def validate_task_spec(path: Path) -> dict:
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
    return task


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


def resolve_harnesses(catalog: dict, args: argparse.Namespace) -> list[dict]:
    entries = catalog["harnesses"]
    by_id = {entry["id"]: entry for entry in entries}
    if args.harness:
        if args.harness not in by_id:
            raise ValueError(f"harness is not allowlisted: {args.harness}")
        return [by_id[args.harness]]
    if args.preflight and not args.interactive:
        return entries
    if args.interactive or sys.stdin.isatty():
        choices = [(entry["id"], entry.get("display_name", entry["id"])) for entry in entries]
        return [by_id[choose_index(choices, "请选择 harness:")]]
    # Explicit model-only CI invocations from older packages continue to use
    # Codex, while participant-facing interactive runs always ask first.
    return [by_id["codex"]]


def find_harness_executable(harness: dict) -> str | None:
    """Resolve an approved CLI, including the Codex binary bundled on macOS."""
    executable_env = harness.get("executable_env", "")
    override = os.environ.get(executable_env) if executable_env else None
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    discovered = shutil.which(harness.get("executable", ""))
    if discovered:
        return discovered
    if harness.get("id") == "codex":
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    return None


def resolve_models(catalog: dict, args: argparse.Namespace) -> list[dict]:
    models = catalog["models"]
    all_by_id = {model["id"]: model for model in models}
    by_id = dict(all_by_id)
    selected_harness = getattr(args, "harness_id", None)
    if selected_harness:
        models = [
            model
            for model in models
            if selected_harness in model.get("supported_harnesses", CANONICAL_HARNESS_IDS)
        ]
        by_id = {model["id"]: model for model in models}
        if not models:
            raise ValueError(f"selected harness has no compatible approved models: {selected_harness}")
    if args.preflight and not args.model and not args.interactive:
        return models
    if args.model:
        if args.model not in by_id:
            if args.model in all_by_id and selected_harness:
                raise ValueError(f"model {args.model} is not compatible with harness {selected_harness}")
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


def build_provider_parameters(model: dict, profile: dict) -> dict:
    """Translate canonical profile parameters into the provider wire format."""
    adapter_name = model.get("adapter")
    adapter = PROVIDER_ADAPTERS.get(adapter_name)
    if adapter is None:
        raise ValueError(f"no provider adapter is registered for {adapter_name!r}")
    parameters = profile.get("parameters", {})
    if parameters != FIXED_PARAMETERS:
        raise ValueError("provider adapters only accept the fixed canonical parameters")
    return adapter(parameters)


def build_provider_request(model: dict, profile: dict) -> dict:
    """Return a serializable request fragment for task-specific adapters."""
    return {
        "provider": model["provider"],
        "adapter": model["adapter"],
        "endpoint": model["endpoint"],
        "base_url": model["base_url"],
        "model": model["model_name"],
        "parameters": build_provider_parameters(model, profile),
    }


def build_environment(
    model: dict,
    profile: dict,
    credential: str | None,
    task_spec: Path,
    *,
    workspace: Path | None = None,
    output_dir: Path | None = None,
    roll_dir: Path | None = None,
    fixtures_dir: Path | None = None,
    isolation_manifest: Path | None = None,
    harness: dict | None = None,
) -> dict[str, str]:
    """Build a minimal child environment with explicit provider and roll paths."""
    native_parameters = build_provider_parameters(model, profile)
    provider_request = build_provider_request(model, profile)
    env = {"PATH": os.environ.get("PATH", "")}
    for executable_env in ("CODEX_BIN", "CLAUDE_BIN"):
        if os.environ.get(executable_env):
            env[executable_env] = os.environ[executable_env]
    credential_env = model.get("credential_env")
    if credential_env and credential:
        env[credential_env] = credential
        # Harness scripts consume one provider-neutral value. The provider
        # variable is retained for task-specific adapters and diagnostics.
        env["VERIFORGE_API_KEY"] = credential
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
            "VERIFORGE_NATIVE_PARAMETERS_JSON": json.dumps(native_parameters, sort_keys=True),
            "VERIFORGE_PROVIDER_REQUEST_JSON": json.dumps(provider_request, sort_keys=True),
            "VERIFORGE_HARNESS_ID": str((harness or {}).get("id", "")),
            "VERIFORGE_HARNESS_PROTOCOL": str((harness or {}).get("protocol", "")),
            "VERIFORGE_API_BASE_URL": str(model.get("base_url", "")),
        }
    )
    if workspace is not None:
        workspace = workspace.resolve()
        env["VERIFORGE_WORKSPACE"] = str(workspace)
        env["PWD"] = str(workspace)
        env["HOME"] = str((roll_dir or workspace) / "home")
        env["XDG_CONFIG_HOME"] = str((roll_dir or workspace) / "config")
        env["XDG_CACHE_HOME"] = str((roll_dir or workspace) / "cache")
    if output_dir is not None:
        env["VERIFORGE_OUTPUT_DIR"] = str(output_dir.resolve())
    if roll_dir is not None:
        env["VERIFORGE_ROLL_DIR"] = str(roll_dir.resolve())
    if fixtures_dir is not None:
        env["VERIFORGE_FIXTURES_DIR"] = str(fixtures_dir.resolve())
    if isolation_manifest is not None:
        env["VERIFORGE_ISOLATION_MANIFEST"] = str(isolation_manifest.resolve())
    return env


def redact_diagnostic(text: str, credential: str | None) -> str:
    """Keep failure diagnostics useful without persisting or echoing a key."""
    if credential:
        text = text.replace(credential, "<redacted>")
    text = SECRET_VALUE_PATTERN.sub(lambda match: match.group(1) + "=<redacted>", text)
    return text.strip()[-2000:]


def infer_workspace_source(task_spec: Path) -> Path:
    """Find the generated package root without assuming the caller's cwd."""
    for candidate in (task_spec.parent, *task_spec.parents):
        if (candidate / "01-task").is_dir() and (candidate / "03-runner" / "models.yaml").is_file():
            return candidate
    return task_spec.parent


def stage_workspace(source: Path, workspace: Path, task_spec: Path) -> Path:
    """Copy a clean package into a roll workspace and protect the task spec."""
    source = source.resolve()
    workspace = workspace.resolve()
    if not source.is_dir():
        raise ValueError(f"workspace source is not a directory: {source}")
    try:
        relative_spec = task_spec.resolve().relative_to(source)
    except ValueError:
        relative_spec = Path("task.yaml")

    def ignore_generated(_path: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {"results", ".git", "__pycache__", ".pytest_cache"}}

    shutil.copytree(source, workspace, ignore=ignore_generated)
    staged_spec = workspace / relative_spec
    if not staged_spec.is_file():
        staged_spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task_spec, staged_spec)
    staged_spec.chmod(staged_spec.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return staged_spec


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def redact_text(text: str, credential: str | None) -> str:
    """Redact a complete log while retaining enough context for diagnosis."""
    if credential:
        text = text.replace(credential, "<redacted>")
    return SECRET_VALUE_PATTERN.sub(lambda match: match.group(1) + "=<redacted>", text)


def write_redacted_log(path: Path, text: str | bytes | None, credential: str | None) -> None:
    path.write_text(redact_text(_as_text(text), credential), encoding="utf-8")


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int | float | None,
) -> tuple[int, str, str, bool]:
    """Run one command and terminate its process group on timeout."""
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - Windows is not the supported local harness
            process.kill()
        stdout, stderr = process.communicate()
        return 124, _as_text(exc.stdout) + _as_text(stdout), _as_text(exc.stderr) + _as_text(stderr), True


def run_roll(
    model: dict,
    profile: dict,
    credential: str | None,
    args: argparse.Namespace,
    index: int,
    config_digest: str,
    task_spec_digest: str,
    harness: dict | None = None,
) -> dict:
    started = utc_now()

    roll_dir = args.results_dir.resolve() / f"roll-{index}"
    workspace = roll_dir / "workspace"
    output_dir = workspace / "outputs"
    logs_dir = roll_dir / "logs"
    scorer_result_path = roll_dir / "scorer-result.json"
    roll_dir.mkdir(parents=True, exist_ok=False)
    logs_dir.mkdir(parents=True, exist_ok=True)
    for private_dir in (roll_dir / "home", roll_dir / "config", roll_dir / "cache"):
        private_dir.mkdir(parents=True, exist_ok=True)

    staged_spec = stage_workspace(args.workspace_source, workspace, args.task_spec)
    output_dir.mkdir(parents=True, exist_ok=True)
    isolation_manifest = load_isolation_manifest(workspace, args.task_id)
    fixture_before = fixture_hashes(workspace, isolation_manifest)
    fixture_root = (
        workspace / isolation_manifest["fixture_paths"][0]
        if isolation_manifest.get("fixture_paths")
        else workspace / "02-evaluation" / "fixtures"
    )
    staged_spec_hash = file_hash(staged_spec)
    command = resolve_agent_command(
        workspace,
        args.agent_command,
        getattr(args, "harness_id", None),
    ) if not args.dry_run else None
    scorer_command = (
        resolve_scorer_command(workspace, output_dir, getattr(args, "scorer_command", None))
        if not args.dry_run
        else None
    )
    native_parameters = build_provider_parameters(model, profile)
    provider_request = build_provider_request(model, profile)
    record = {
        "task_id": args.task_id,
        "harness_id": (harness or {}).get("id"),
        "harness": (harness or {}).get("display_name"),
        "model_id": model["id"],
        "model_name": model["model_name"],
        "provider": model["provider"],
        "adapter": model["adapter"],
        "profile_id": profile["id"],
        "parameters": profile.get("parameters", {}),
        "native_parameters": native_parameters,
        "provider_request": provider_request,
        "roll": index,
        "runner_version": RUNNER_VERSION,
        "models_hash": config_digest,
        "task_spec_hash": task_spec_digest,
        "staged_task_spec_hash": staged_spec_hash,
        "fixture_hashes": fixture_before,
        "fixture_paths": list(fixture_before),
        "isolation_manifest": isolation_manifest.get("manifest_path"),
        "benchmark_status": "verified",
        "started_at": started,
        "roll_dir": str(roll_dir),
        "workspace": str(workspace),
        "task_spec": str(staged_spec),
        "output_dir": str(output_dir),
        "scorer_result": str(scorer_result_path),
        "status": "dry_run" if args.dry_run else "pending",
    }
    if args.dry_run:
        scorer_result_path.write_text(
            json.dumps({"status": "not_run", "reason": "dry_run"}, indent=2) + "\n",
            encoding="utf-8",
        )
        record["ended_at"] = utc_now()
        return record

    env = build_environment(
        model,
        profile,
        credential,
        staged_spec,
        workspace=workspace,
        output_dir=output_dir,
        roll_dir=roll_dir,
        fixtures_dir=fixture_root,
        isolation_manifest=(
            Path(isolation_manifest["manifest_path"])
            if isolation_manifest.get("manifest_path")
            else None
        ),
        harness=harness,
    )
    env["VERIFORGE_SCORER_RESULT"] = str(roll_dir / "scorer-result.json")
    print(f"[veriforge] roll {index}/{args.rolls}: starting agent in {workspace}", flush=True)
    try:
        returncode, stdout, stderr, timed_out = run_command(
            command,
            cwd=workspace,
            env=env,
            timeout=profile.get("timeout_seconds"),
        )
    except OSError as exc:
        returncode, stdout, stderr, timed_out = 127, "", str(exc), False

    write_redacted_log(logs_dir / "agent.stdout.log", stdout, credential)
    write_redacted_log(logs_dir / "agent.stderr.log", stderr, credential)
    record["agent_stdout_log"] = str(logs_dir / "agent.stdout.log")
    record["agent_stderr_log"] = str(logs_dir / "agent.stderr.log")
    record["returncode"] = returncode
    record["task_spec_integrity"] = file_hash(staged_spec) == staged_spec_hash
    record["fixture_integrity"] = fixture_hashes(workspace, isolation_manifest) == fixture_before
    integrity_ok = record["task_spec_integrity"] and record["fixture_integrity"]
    record["status"] = "failed" if returncode != 0 or not integrity_ok else "pending"
    if timed_out:
        record["timeout_seconds"] = profile.get("timeout_seconds")
    if returncode != 0:
        for stream_name, stream in (("stdout", stdout), ("stderr", stderr)):
            diagnostic = redact_diagnostic(stream or "", credential)
            if diagnostic:
                record[f"agent_{stream_name}_tail"] = diagnostic
                print(f"[veriforge] agent {stream_name}: {diagnostic}", file=sys.stderr, flush=True)
        print(f"[veriforge] roll {index}/{args.rolls}: agent failed (exit {returncode})", flush=True)
    else:
        print(f"[veriforge] roll {index}/{args.rolls}: agent finished", flush=True)

    if returncode != 0:
        record["scorer_status"] = "skipped_agent_failed"
        scorer_result_path.write_text(
            json.dumps({"status": "failed", "reason": "agent failed before scoring"}, indent=2) + "\n",
            encoding="utf-8",
        )
    elif not integrity_ok:
        record["scorer_status"] = "skipped_integrity_failure"
        scorer_result_path.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "task spec or fixture integrity changed during agent run",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[veriforge] roll {index}/{args.rolls}: integrity check failed", flush=True)
    elif scorer_command:
        print(f"[veriforge] roll {index}/{args.rolls}: scoring", flush=True)
        try:
            scorer_returncode, scorer_stdout, scorer_stderr, scorer_timed_out = run_command(
                scorer_command,
                cwd=workspace,
                env=env,
                timeout=profile.get("timeout_seconds"),
            )
        except OSError as exc:
            scorer_returncode, scorer_stdout, scorer_stderr, scorer_timed_out = 127, "", str(exc), False
        write_redacted_log(logs_dir / "scorer.stdout.log", scorer_stdout, credential)
        write_redacted_log(logs_dir / "scorer.stderr.log", scorer_stderr, credential)
        scorer_payload = None
        scorer_source = scorer_result_path.read_text(encoding="utf-8") if scorer_result_path.exists() else scorer_stdout
        try:
            parsed = json.loads(scorer_source)
            if isinstance(parsed, dict):
                scorer_payload = parsed
        except json.JSONDecodeError:
            scorer_payload = None
        if scorer_payload is None:
            scorer_payload = {
                "status": "failed",
                "reason": "scorer did not emit a JSON object",
                "returncode": scorer_returncode,
            }
        scorer_result_path.write_text(
            json.dumps(scorer_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        record["scorer_status"] = "passed" if scorer_returncode == 0 and scorer_payload.get("passed") is True else "failed"
        record["scorer_returncode"] = scorer_returncode
        record["score"] = scorer_payload.get("score")
        record["passed"] = scorer_payload.get("passed") is True
        record["scorer_stdout_log"] = str(logs_dir / "scorer.stdout.log")
        record["scorer_stderr_log"] = str(logs_dir / "scorer.stderr.log")
        record["status"] = "passed" if record["scorer_status"] == "passed" else "failed"
        if scorer_timed_out:
            record["scorer_timeout_seconds"] = profile.get("timeout_seconds")
        print(
            f"[veriforge] roll {index}/{args.rolls}: score={record.get('score')} status={record['status']}",
            flush=True,
        )
    record["ended_at"] = utc_now()
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one allowlisted VeriForge model for N rolls")
    parser.add_argument("--models-file", type=Path, default=Path("03-runner/models.yaml"))
    parser.add_argument("--task-id", default="unversioned-task")
    parser.add_argument("--model")
    parser.add_argument("--harness", choices=sorted(CANONICAL_HARNESS_IDS))
    parser.add_argument(
        "--harnesses-file",
        type=Path,
        default=Path("03-runner/harnesses.yaml"),
        help="organizer-approved CC/Codex harness matrix",
    )
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--rolls", type=int)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--results-dir",
        type=Path,
        help="result directory (defaults to results/<model>-<UTC timestamp>)",
    )
    parser.add_argument("--task-spec", type=Path, default=Path("01-task/task.yaml"))
    parser.add_argument(
        "--workspace-source",
        type=Path,
        help="clean task package to copy for every roll (defaults to the generated package root)",
    )
    parser.add_argument(
        "--scorer-command",
        nargs="+",
        help="optional scorer command; it runs in each roll workspace after a successful agent",
    )
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
    harness_path = args.harnesses_file.resolve()
    if not harness_path.is_file() and args.harnesses_file == Path("03-runner/harnesses.yaml"):
        candidate_source = args.workspace_source or infer_workspace_source(args.task_spec.resolve())
        candidate = candidate_source / "03-runner" / "harnesses.yaml"
        if candidate.is_file():
            harness_path = candidate.resolve()
    if harness_path.is_file():
        harness_catalog = load_harness_catalog(harness_path)
    elif args.harness:
        raise ValueError(f"harnesses file not found: {harness_path}")
    else:
        # Old developer-only packages may not have the new matrix yet. They
        # continue to work through the legacy provider adapter; generated
        # participant packages always ship harnesses.yaml.
        harness_catalog = {
            "harnesses": [dict(value, id=key) for key, value in CANONICAL_HARNESSES.items()],
        }
    roll_policy = catalog.get("roll_policy", {})
    if args.rolls is None:
        args.rolls = roll_policy.get("default_rolls", 1)
    min_rolls = roll_policy.get("min_rolls", 1)
    max_rolls = roll_policy.get("max_rolls", args.rolls)
    if args.rolls < min_rolls or args.rolls > max_rolls:
        raise ValueError(f"--rolls must be between {min_rolls} and {max_rolls}")

    harnesses = resolve_harnesses(harness_catalog, args)
    harness = harnesses[0]
    # A matrix-only preflight validates every harness; interactive and explicit
    # runs select exactly one harness and can filter the model choices.
    args.harness_id = harness.get("id") if harness_path.is_file() and len(harnesses) == 1 else None
    if len(harnesses) == 1 and not args.dry_run:
        executable = find_harness_executable(harness)
        if not executable:
            variable = harness.get("executable_env", "")
            raise ValueError(
                f"{harness.get('display_name', harness['id'])} CLI not found; "
                f"install {harness.get('executable')} or set {variable}"
            )
        if harness.get("executable_env"):
            os.environ[harness["executable_env"]] = executable
    models = resolve_models(catalog, args)
    args.task_spec = args.task_spec.resolve()
    if not args.task_spec.is_file():
        if args.preflight:
            print(f"task spec not found: {args.task_spec}", file=sys.stderr)
            return 2
        raise ValueError(f"task spec not found: {args.task_spec}")
    task = validate_task_spec(args.task_spec)
    if args.task_id == "unversioned-task" and isinstance(task.get("task_id"), str):
        args.task_id = task["task_id"]
    args.workspace_source = (args.workspace_source or infer_workspace_source(args.task_spec)).resolve()
    if not args.workspace_source.is_dir():
        raise ValueError(f"workspace source not found: {args.workspace_source}")
    validate_generated_package(
        args.workspace_source,
        args.agent_command,
        args.scorer_command,
        args.harness_id if harness_path.is_file() else None,
    )
    if args.preflight:
        # Credential mode is per_selected_model. A matrix-only preflight may
        # report all five variables, but only an explicitly selected model can
        # make the preflight fail for a missing credential.
        selected_for_check = bool(args.model or args.interactive)
        missing = check_credentials(models) if selected_for_check else []
        for entry in harnesses:
            found = find_harness_executable(entry)
            state = "ready" if found else "missing"
            print(f"{entry['id']}: harness {state} ({entry.get('executable')})")
        for model in models:
            variable = model.get("credential_env")
            state = "ready" if not variable or os.environ.get(variable) else "missing"
            print(f"{model['id']}: credential {state} ({variable or 'none'})")
        if missing and not args.dry_run:
            return 2
        return 0

    model = models[0]
    if args.results_dir is None:
        args.results_dir = default_results_dir(model["id"])
    print(f"[veriforge] 已选择 harness: {harness.get('display_name', harness['id'])} ({harness['id']})")
    print(f"[veriforge] 已选择模型: {model.get('display_name', model['id'])} ({model['id']})")
    print(f"[veriforge] rollout 次数: {args.rolls}")
    print(f"[veriforge] 结果目录: {args.results_dir}")
    credential_env = model.get("credential_env")
    credential = os.environ.get(credential_env) if credential_env else None
    if not credential and not args.dry_run:
        if not credential_env:
            raise ValueError(f"model {model['id']} has no credential_env")
        if not sys.stdin.isatty():
            raise ValueError(f"missing runtime credential: {credential_env}")
        credential = getpass.getpass(
            f"请输入 {model.get('display_name', model['id'])} 的 API Key（输入内容不会显示）: "
        )
        if not credential:
            raise ValueError("API Key cannot be empty")

    digest = catalog_hash(catalog)
    task_spec_digest = hashlib.sha256(args.task_spec.read_bytes()).hexdigest()
    args.results_dir = args.results_dir.resolve()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.results_dir / "run-manifest.json"
    if not args.dry_run and manifest_path.exists():
        raise ValueError(f"results directory already contains a run manifest: {manifest_path}")
    if not args.dry_run and any(args.results_dir.glob("roll-*")):
        raise ValueError(f"results directory already contains roll artifacts: {args.results_dir}")
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
                harness=harness,
            )
        )

    output = manifest_path
    output.write_text(
        json.dumps(
            {
                "task_id": args.task_id,
                "harness_id": harness["id"],
                "harness": harness.get("display_name", harness["id"]),
                "benchmark_status": "verified",
                "runs": records,
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if args.dry_run:
        print(f"[veriforge] 完成: 已生成 {len(records)} 个 dry-run 记录（未调用模型或评分）")
    else:
        passed_count = sum(record["status"] == "passed" for record in records)
        print(f"[veriforge] 完成: {passed_count}/{len(records)} rolls passed")
    print(f"[veriforge] 运行清单: {output}")
    return 0 if all(record["status"] in {"dry_run", "passed"} for record in records) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
