#!/usr/bin/env python3
"""Participant runner for one approved model and N isolated harness rollouts."""

from __future__ import annotations

import argparse
import atexit
from contextlib import suppress
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import re
import shutil
import signal
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by preflight users
    raise SystemExit("PyYAML is required to read models.yaml") from exc


RUNNER_VERSION = "veriforge-runner/v2.5"
SECRET_VALUE_PATTERN = re.compile(r"(?i)(api[_ -]?key|password|secret|cookie|access[_ -]?token)\s*[:=]\s*[^\s,;]+")
PLACEHOLDER_MARKER = "REPLACE_WITH_"
FIXED_PARAMETERS = {"reasoning_effort": "max", "max_output_tokens": 32768}
ISOLATION_MANIFEST_VERSION = "veriforge-isolation-manifest/v1"
EVALUATION_DIR = "02-evaluation"
CONTROL_PATHS = {
    "scorer": "02-evaluation/scorer.py",
    "rubric": "02-evaluation/rubric.yaml",
    "reference_answer": "02-evaluation/reference_answer",
    "validators": "02-evaluation/validators",
    "harnesses": "03-runner/harnesses.yaml",
    "models": "03-runner/models.yaml",
    "allowlist": "03-runner/harness.allowlist.yaml",
    "dependency_manifest": "03-runner/dependency-manifest.yaml",
    "isolation_manifest": "03-runner/isolation-manifest.yaml",
    "cc_agent": "03-runner/cc_agent.sh",
    "codex_agent": "03-runner/codex_agent.sh",
    "provider_agent": "03-runner/provider_agent.py",
}
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
        "response_format": {"type": "json_object"},
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
        timeout_seconds = profile.get("timeout_seconds")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError(f"model {model_id} default profile must declare a finite positive timeout_seconds")
        retries = profile.get("retries")
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            raise ValueError(f"model {model_id} default profile retries must be a non-negative integer")
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
    """Hash a path by relative names, file contents, and permission bits."""
    digest = hashlib.sha256()
    if root.is_symlink():
        digest.update(b"symlink\0")
        digest.update(str(root.readlink()).encode("utf-8"))
        return digest.hexdigest()
    if root.is_file():
        digest.update(b"file\0")
        digest.update(root.name.encode("utf-8"))
        digest.update(str(stat.S_IMODE(root.stat().st_mode)).encode("ascii"))
        digest.update(root.read_bytes())
        return digest.hexdigest()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"symlink\0" + relative + b"\0")
            digest.update(str(path.readlink()).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"directory\0" + relative + b"\0")
            digest.update(str(stat.S_IMODE(path.stat().st_mode)).encode("ascii"))
        elif path.is_file():
            digest.update(b"file\0" + relative + b"\0")
            digest.update(str(stat.S_IMODE(path.stat().st_mode)).encode("ascii"))
            digest.update(b"\0" + path.read_bytes())
    return digest.hexdigest()


def _relative_path(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path is outside workspace: {path}") from exc


def _path_matches(relative: Path, roots: list[Path]) -> bool:
    return any(relative == root or root in relative.parents for root in roots)


def protected_workspace_hash(workspace: Path, mutable_paths: list[str]) -> str:
    """Hash every workspace entry outside the declared mutable paths."""
    root = workspace.resolve()
    mutable = [Path(path) for path in mutable_paths]
    digest = hashlib.sha256()
    entries = sorted(
        item
        for item in root.rglob("*")
        if item.is_file() or item.is_dir() or item.is_symlink()
    )
    for path in entries:
        relative = path.relative_to(root)
        if _path_matches(relative, mutable):
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0" + str(path.readlink()).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"directory\0")
            digest.update(str(stat.S_IMODE(path.stat().st_mode)).encode("ascii"))
        else:
            digest.update(b"file\0")
            digest.update(str(stat.S_IMODE(path.stat().st_mode)).encode("ascii"))
            digest.update(b"\0" + path.read_bytes())
    return digest.hexdigest()


def make_read_only(path: Path) -> None:
    """Remove write bits from a staged protected path."""
    if not path.exists() and not path.is_symlink():
        return
    targets = [path]
    if path.is_dir() and not path.is_symlink():
        targets.extend(path.rglob("*"))
    for target in targets:
        mode = stat.S_IMODE(target.stat().st_mode)
        target.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def path_hashes(root: Path, entries: dict[str, str], *, required: bool) -> dict[str, str]:
    """Hash named files/directories and fail closed for required controls."""
    hashes: dict[str, str] = {}
    for label, relative in entries.items():
        path = root / relative
        if not path.exists() and not path.is_symlink():
            if required:
                raise ValueError(f"required integrity path is missing: {relative}")
            continue
        if path.is_symlink():
            raise ValueError(f"integrity path must not be a symlink: {relative}")
        hashes[label] = tree_hash(path)
    return hashes


def package_integrity_hashes(
    source: Path,
    task_spec: Path,
    manifest: dict,
    *,
    required: bool,
) -> dict[str, str]:
    """Hash all benchmark controls before and after an Agent rollout."""
    source = source.resolve()
    hashes: dict[str, str] = {}
    hashes["task_spec"] = tree_hash(task_spec.resolve())
    control_paths = dict(CONTROL_PATHS)
    for index, relative in enumerate(manifest.get("fixture_paths", []), start=1):
        control_paths[f"fixture:{index}:{relative}"] = relative
    hashes.update(path_hashes(source, control_paths, required=required))
    return hashes


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
    manifest["manifest_path"] = str(path)
    return manifest


def validate_manifest_paths(
    workspace: Path,
    manifest: dict,
    task_spec: Path,
    *,
    strict: bool = True,
) -> None:
    """Validate and materialize the declared read-only/mutable boundary."""
    root = workspace.resolve()
    read_only = [Path(path) for path in manifest.get("read_only_paths", [])]
    mutable = [Path(path) for path in manifest.get("mutable_paths", [])]
    if any(
        _path_matches(left, [right]) or _path_matches(right, [left])
        for left in read_only
        for right in mutable
    ):
        raise ValueError("isolation manifest paths cannot be both read-only and mutable")

    for relative in [*read_only, *mutable]:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"isolation manifest path escapes workspace: {relative}")
        if relative == Path("."):
            raise ValueError("isolation manifest paths must not be the workspace root")
        candidate = root / relative
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise ValueError(f"isolation manifest path escapes workspace: {relative}") from exc

    for relative in read_only:
        candidate = root / relative
        if not candidate.exists() and not candidate.is_symlink():
            if strict:
                raise ValueError(f"declared read-only path is missing: {relative}")
            continue
        make_read_only(candidate)

    for relative in mutable:
        candidate = root / relative
        if candidate.exists() and candidate.is_symlink():
            raise ValueError(f"mutable path must not be a symlink: {relative}")
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)

    staged_spec = _relative_path(task_spec, root)
    if strict and not _path_matches(staged_spec, read_only):
        raise ValueError("task spec must be declared in isolation-manifest.read_only_paths")

    output_dir = root / "outputs"
    if strict and not _path_matches(_relative_path(output_dir, root), mutable):
        raise ValueError("outputs must be covered by isolation-manifest.mutable_paths")
    if strict:
        for fixture in manifest.get("fixture_paths", []):
            if not _path_matches(Path(fixture), read_only):
                raise ValueError(f"fixture path must be read-only: {fixture}")


def fixture_hashes(workspace: Path, manifest: dict, *, strict: bool = False) -> dict[str, str]:
    hashes = {}
    for relative in manifest.get("fixture_paths", []):
        path = workspace / relative
        if not path.exists() and not path.is_symlink():
            if strict:
                raise ValueError(f"declared fixture path is missing: {relative}")
            hashes[relative] = "<missing>"
            continue
        hashes[relative] = tree_hash(path)
    return hashes


def resolve_agent_command(
    workspace: Path,
    explicit: list[str] | None,
    harness_id: str | None = None,
) -> list[str]:
    """Resolve the selected harness wrapper and its trusted provider adapter."""
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


def resolve_scorer_command(evaluation_dir: Path, output_dir: Path, explicit: list[str] | None) -> list[str]:
    """Use the trusted per-roll scorer unless a developer override is supplied."""
    if explicit:
        return explicit
    scorer = evaluation_dir / "scorer.py"
    if not scorer.is_file():
        raise ValueError("trusted scorer not found in the evaluation directory")
    return [sys.executable, str(scorer), "--outputs", str(output_dir)]


def validate_generated_package(
    source: Path,
    explicit_agent: list[str] | None,
    explicit_scorer: list[str] | None,
    harness_id: str | None = None,
    *,
    participant_mode: bool = True,
) -> None:
    """Fail preflight before a participant enters a key if generated tools are missing."""
    if participant_mode:
        if explicit_agent or explicit_scorer:
            raise ValueError("agent/scorer command overrides require --developer-mode")
        required = (
            source / "02-evaluation" / "scorer.py",
            source / "02-evaluation" / "rubric.yaml",
            source / "02-evaluation" / "reference_answer",
            source / "02-evaluation" / "validators",
            source / "03-runner" / "harness.allowlist.yaml",
            source / "03-runner" / "dependency-manifest.yaml",
            source / "03-runner" / "isolation-manifest.yaml",
            source / "03-runner" / "provider_agent.py",
        )
        missing = [str(path.relative_to(source)) for path in required if not path.exists()]
        if missing:
            raise ValueError(f"participant package is missing required integrity assets: {', '.join(missing)}")
        harnesses = ("cc", "codex") if harness_id is None else (harness_id,)
        for selected in harnesses:
            adapter = source / "03-runner" / f"{selected}_agent.sh"
            if not adapter.is_file() or not os.access(adapter, os.X_OK):
                raise ValueError(f"participant package is missing executable adapter: {adapter.name}")
        return

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


def stage_workspace(
    source: Path,
    workspace: Path,
    task_spec: Path,
    manifest: dict,
    *,
    strict: bool = True,
) -> Path:
    """Copy only task/runtime inputs; evaluation assets stay outside the Agent workspace."""
    source = source.resolve()
    workspace = workspace.resolve()
    if not source.is_dir():
        raise ValueError(f"workspace source is not a directory: {source}")
    try:
        relative_spec = task_spec.resolve().relative_to(source)
    except ValueError:
        relative_spec = Path("task.yaml")

    def ignore_generated(path: str, names: list[str]) -> set[str]:
        current = Path(path).resolve()
        ignored = {name for name in names if name in {"results", ".git", "__pycache__", ".pytest_cache"}}
        if current == source and EVALUATION_DIR in names:
            ignored.add(EVALUATION_DIR)
        return ignored

    shutil.copytree(source, workspace, ignore=ignore_generated)

    # Fixtures are the only evaluation assets the Agent is allowed to inspect.
    for relative in manifest.get("fixture_paths", []):
        fixture_source = source / relative
        fixture_target = workspace / relative
        if not fixture_source.exists() and not fixture_source.is_symlink():
            if strict:
                raise ValueError(f"declared fixture path is missing: {relative}")
            continue
        if fixture_source.is_symlink():
            raise ValueError(f"declared fixture path must not be a symlink: {relative}")
        fixture_target.parent.mkdir(parents=True, exist_ok=True)
        if fixture_source.is_dir() and not fixture_source.is_symlink():
            shutil.copytree(fixture_source, fixture_target, dirs_exist_ok=True)
        else:
            shutil.copy2(fixture_source, fixture_target)

    staged_spec = workspace / relative_spec
    if not staged_spec.is_file():
        staged_spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task_spec, staged_spec)
    make_read_only(staged_spec)
    return staged_spec


def create_source_snapshot(source: Path) -> Path:
    """Freeze the participant package for the lifetime of one benchmark run."""
    source = source.resolve()
    parent = Path(tempfile.mkdtemp(prefix="veriforge-source-"))
    snapshot = parent / source.name

    def ignore_runtime(_path: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in {"results", ".git", "__pycache__", ".pytest_cache"}
        }

    try:
        shutil.copytree(source, snapshot, ignore=ignore_runtime)
    except Exception:
        with suppress(Exception):
            shutil.rmtree(parent)
        raise
    return snapshot


def remove_source_snapshot(snapshot: Path) -> None:
    """Best-effort cleanup for the immutable per-run package snapshot."""
    remove_evaluation_source(snapshot)
    with suppress(OSError):
        snapshot.parent.rmdir()


def stage_evaluation_source(source: Path, evaluation_dir: Path) -> Path:
    """Create a trusted, per-roll copy of evaluation assets outside Agent cwd."""
    evaluation_source = source.resolve() / EVALUATION_DIR
    if not evaluation_source.is_dir():
        raise ValueError("evaluation source not found: 02-evaluation")
    shutil.copytree(
        evaluation_source,
        evaluation_dir,
        dirs_exist_ok=True,
        ignore=lambda _path, names: {name for name in names if name in {"__pycache__", ".pytest_cache"}},
    )
    make_read_only(evaluation_dir)
    return evaluation_dir


def remove_evaluation_source(evaluation_dir: Path) -> None:
    """Remove the temporary evaluation copy even if an Agent changed modes."""
    if not evaluation_dir.exists() and not evaluation_dir.is_symlink():
        return
    if evaluation_dir.is_symlink():
        evaluation_dir.unlink()
        return
    evaluation_dir.chmod(stat.S_IRWXU)
    for root, directories, files in os.walk(evaluation_dir, followlinks=False):
        root_path = Path(root)
        root_path.chmod(stat.S_IRWXU)
        for name in directories:
            target = root_path / name
            if not target.is_symlink():
                target.chmod(stat.S_IRWXU)
        for name in files:
            target = root_path / name
            if not target.is_symlink():
                target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    shutil.rmtree(evaluation_dir)


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


def _event_summary(payload: dict) -> str:
    """Turn common Codex/Claude stream events into concise terminal progress."""
    event_type = str(payload.get("type", "event"))
    item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    item_type = str(item.get("type", ""))
    message = payload.get("message") if isinstance(payload.get("message"), dict) else None
    content = message.get("content") if message else None
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))
            if block_type in {"tool_use", "tool_result"}:
                name = block.get("name") or block.get("tool_name") or block.get("id")
                return f"{event_type} {block_type}: {name}" if name else f"{event_type} {block_type}"
            if block_type in {"text", "thinking"} and isinstance(block.get("text"), str):
                compact = " ".join(block["text"].split())
                return f"{event_type} {block_type}: {compact[:500]}"
    block = payload.get("content_block") if isinstance(payload.get("content_block"), dict) else None
    if block and block.get("type") in {"tool_use", "tool_result"}:
        name = block.get("name") or block.get("tool_name") or block.get("id")
        return f"{event_type} {block['type']}: {name}" if name else f"{event_type} {block['type']}"
    if item_type in {"command_execution", "bash", "shell_command"}:
        command = item.get("command") or item.get("input") or item.get("cmd")
        return f"{event_type} {item_type}: {str(command).strip()}" if command else f"{event_type} {item_type}"
    if item_type in {"file_change", "file_read", "tool_use", "tool_result"}:
        name = item.get("name") or item.get("tool_name") or item.get("path") or item.get("file")
        return f"{event_type} {item_type}: {name}" if name else f"{event_type} {item_type}"
    text = item.get("text") or item.get("delta") or payload.get("text")
    if isinstance(text, str) and text.strip():
        compact = " ".join(text.split())
        return f"{event_type}: {compact[:500]}"
    return event_type if event_type != "event" else json.dumps(payload, ensure_ascii=False)[:500]


def _live_line(line: str) -> str:
    """Format one child-process line without hiding raw non-JSON output."""
    stripped = line.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(payload, dict):
        return _event_summary(payload)
    return stripped


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int | float | None,
    live_label: str | None = None,
    credential: str | None = None,
    heartbeat_seconds: int | float | None = None,
    stream_stdout: bool = True,
) -> tuple[int, str, str, bool]:
    """Run one command, stream progress, and terminate its process group on timeout."""
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=(os.name == "posix"),
    )

    streams: Queue[tuple[str, str | None]] = Queue()

    def pump(name: str, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                streams.put((name, line))
        finally:
            with suppress(Exception):
                stream.close()
            streams.put((name, None))

    threads = [
        threading.Thread(target=pump, args=(name, stream), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for thread in threads:
        thread.start()

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    finished_streams: set[str] = set()
    started = time.monotonic()
    deadline = started + float(timeout) if timeout is not None else None
    next_heartbeat = started + float(heartbeat_seconds) if heartbeat_seconds else None
    timed_out = False

    def stop_process() -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - Windows is not the supported local harness
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:  # pragma: no cover - Windows is not the supported local harness
                process.kill()
            process.wait()

    while len(finished_streams) < 2:
        now = time.monotonic()
        if deadline is not None and not timed_out and now >= deadline and process.poll() is None:
            timed_out = True
            if live_label:
                print(f"[veriforge][{live_label}] timeout reached; sending SIGTERM", flush=True)
            stop_process()
            if live_label:
                print(f"[veriforge][{live_label}] process stopped after timeout escalation", flush=True)
        if (
            live_label
            and next_heartbeat is not None
            and now >= next_heartbeat
            and process.poll() is None
        ):
            elapsed = int(now - started)
            limit = f"/{int(timeout)}s" if timeout is not None else ""
            print(f"[veriforge][{live_label}] still running ({elapsed}s{limit})", flush=True)
            next_heartbeat = now + float(heartbeat_seconds)
        wait_for = 0.25
        if next_heartbeat is not None:
            wait_for = min(wait_for, max(0.01, next_heartbeat - now))
        if deadline is not None and not timed_out:
            wait_for = min(wait_for, max(0.01, deadline - now))
        try:
            name, line = streams.get(timeout=wait_for)
        except Empty:
            continue
        if line is None:
            finished_streams.add(name)
            continue
        if name == "stdout":
            stdout_parts.append(line)
        else:
            stderr_parts.append(line)
        if live_label and (name == "stderr" or stream_stdout):
            rendered = redact_text(_live_line(line), credential)
            if rendered:
                print(f"[veriforge][{live_label}][{name}] {rendered}", flush=True)

    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=1)
    returncode = 124 if timed_out else process.returncode
    return returncode, "".join(stdout_parts), "".join(stderr_parts), timed_out


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

    def step(number: int, message: str) -> None:
        print(f"[veriforge] roll {index}/{args.rolls} step {number}/6: {message}", flush=True)

    roll_dir = args.results_dir.resolve() / f"roll-{index}"
    workspace = roll_dir / "workspace"
    output_dir = workspace / "outputs"
    logs_dir = roll_dir / "logs"
    scorer_result_path = roll_dir / "scorer-result.json"
    roll_dir.mkdir(parents=True, exist_ok=False)
    logs_dir.mkdir(parents=True, exist_ok=True)
    step(1, "prepare isolated roll and benchmark integrity controls")
    for private_dir in (roll_dir / "home", roll_dir / "config", roll_dir / "cache"):
        private_dir.mkdir(parents=True, exist_ok=True)

    participant_mode = bool(getattr(args, "participant_mode", False))
    source_manifest = load_isolation_manifest(args.workspace_source, args.task_id)
    source_integrity_before = package_integrity_hashes(
        args.workspace_source,
        args.task_spec,
        source_manifest,
        required=participant_mode,
    )
    staged_spec = stage_workspace(
        args.workspace_source,
        workspace,
        args.task_spec,
        source_manifest,
        strict=participant_mode,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    isolation_manifest = load_isolation_manifest(workspace, args.task_id)
    validate_manifest_paths(
        workspace,
        isolation_manifest,
        staged_spec,
        strict=participant_mode,
    )
    fixture_before = fixture_hashes(workspace, isolation_manifest, strict=participant_mode)
    fixture_root = (
        workspace / isolation_manifest["fixture_paths"][0]
        if isolation_manifest.get("fixture_paths")
        else workspace / "02-evaluation" / "fixtures"
    )
    evaluation_dir = stage_evaluation_source(
        args.workspace_source,
        Path(tempfile.mkdtemp(prefix="veriforge-evaluation-")),
    )
    evaluation_hash_before = tree_hash(evaluation_dir)
    protected_workspace_before = protected_workspace_hash(
        workspace,
        isolation_manifest.get("mutable_paths", []),
    )
    staged_spec_hash = file_hash(staged_spec)
    command = resolve_agent_command(
        workspace,
        args.agent_command,
        getattr(args, "harness_id", None),
    ) if not args.dry_run else None
    scorer_command = (
        resolve_scorer_command(evaluation_dir, output_dir, getattr(args, "scorer_command", None))
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
        "integrity_hashes": source_integrity_before,
        "scorer_hash": source_integrity_before.get("scorer"),
        "allowlist_hash": source_integrity_before.get("allowlist"),
        "dependency_manifest_hash": source_integrity_before.get("dependency_manifest"),
        "task_spec_hash": task_spec_digest,
        "staged_task_spec_hash": staged_spec_hash,
        "fixture_hashes": fixture_before,
        "fixture_paths": list(fixture_before),
        "evaluation_dir": str(evaluation_dir),
        "evaluation_retained": False,
        "evaluation_hash": evaluation_hash_before,
        "protected_workspace_hash": protected_workspace_before,
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
    step(2, "stage Agent workspace, fixtures, and trusted evaluation copy")
    if args.dry_run:
        scorer_result_path.write_text(
            json.dumps({"status": "not_run", "reason": "dry_run"}, indent=2) + "\n",
            encoding="utf-8",
        )
        remove_evaluation_source(evaluation_dir)
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
    step(3, f"start {(harness or {}).get('display_name', 'Agent')} in {workspace}")
    try:
        returncode, stdout, stderr, timed_out = run_command(
            command,
            cwd=workspace,
            env=env,
            timeout=profile.get("timeout_seconds"),
            live_label=f"roll {index} agent",
            credential=credential,
            heartbeat_seconds=15,
        )
    except OSError as exc:
        returncode, stdout, stderr, timed_out = 127, "", str(exc), False

    write_redacted_log(logs_dir / "agent.stdout.log", stdout, credential)
    write_redacted_log(logs_dir / "agent.stderr.log", stderr, credential)
    record["agent_stdout_log"] = str(logs_dir / "agent.stdout.log")
    record["agent_stderr_log"] = str(logs_dir / "agent.stderr.log")
    record["returncode"] = returncode
    record["task_spec_integrity"] = file_hash(staged_spec) == staged_spec_hash
    fixture_after = fixture_hashes(workspace, isolation_manifest, strict=participant_mode)
    source_integrity_after = package_integrity_hashes(
        args.workspace_source,
        args.task_spec,
        source_manifest,
        required=participant_mode,
    )
    protected_workspace_after = protected_workspace_hash(
        workspace,
        isolation_manifest.get("mutable_paths", []),
    )
    evaluation_hash_after = tree_hash(evaluation_dir)
    record["fixture_integrity"] = fixture_after == fixture_before
    record["integrity_hashes_after"] = source_integrity_after
    record["evaluation_hash_after"] = evaluation_hash_after
    record["protected_workspace_hash_after"] = protected_workspace_after
    record["source_integrity"] = source_integrity_after == source_integrity_before
    record["evaluation_integrity"] = evaluation_hash_after == evaluation_hash_before
    record["workspace_integrity"] = protected_workspace_after == protected_workspace_before
    integrity_ok = (
        record["task_spec_integrity"]
        and record["fixture_integrity"]
        and record["source_integrity"]
        and record["evaluation_integrity"]
        and record["workspace_integrity"]
    )
    record["status"] = "failed" if returncode != 0 or not integrity_ok else "pending"
    if timed_out:
        record["timeout_seconds"] = profile.get("timeout_seconds")
    step(4, "collect Agent output and verify task, fixture, source, evaluation, and workspace integrity")
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
        step(5, "skip scorer because Agent exited with a non-zero status")
        record["scorer_status"] = "skipped_agent_failed"
        scorer_result_path.write_text(
            json.dumps({"status": "failed", "reason": "agent failed before scoring"}, indent=2) + "\n",
            encoding="utf-8",
        )
    elif not integrity_ok:
        step(5, "skip scorer because an integrity check failed")
        record["scorer_status"] = "skipped_integrity_failure"
        scorer_result_path.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "benchmark controls or protected workspace paths changed during agent run",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[veriforge] roll {index}/{args.rolls}: integrity check failed", flush=True)
    elif scorer_command:
        step(5, "run trusted scorer and stream scorer diagnostics")
        print(f"[veriforge] roll {index}/{args.rolls}: scoring", flush=True)
        # The Agent can see the roll directory in legacy/custom adapters. Any
        # pre-existing result is untrusted; scorer stdout is authoritative.
        scorer_result_path.unlink(missing_ok=True)
        scorer_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "VERIFORGE_OUTPUT_DIR": str(output_dir),
        }
        try:
            scorer_returncode, scorer_stdout, scorer_stderr, scorer_timed_out = run_command(
                scorer_command,
                cwd=evaluation_dir,
                env=scorer_env,
                timeout=profile.get("timeout_seconds"),
                live_label=f"roll {index} scorer",
                credential=credential,
                heartbeat_seconds=15,
                stream_stdout=False,
            )
        except OSError as exc:
            scorer_returncode, scorer_stdout, scorer_stderr, scorer_timed_out = 127, "", str(exc), False
        write_redacted_log(logs_dir / "scorer.stdout.log", scorer_stdout, credential)
        write_redacted_log(logs_dir / "scorer.stderr.log", scorer_stderr, credential)
        evaluation_hash_after_scoring = tree_hash(evaluation_dir)
        record["evaluation_hash_after_scoring"] = evaluation_hash_after_scoring
        scorer_integrity = evaluation_hash_after_scoring == evaluation_hash_before
        record["scorer_integrity"] = scorer_integrity
        scorer_payload = None
        try:
            parsed = json.loads(scorer_stdout)
            if isinstance(parsed, dict):
                scorer_payload = parsed
        except json.JSONDecodeError:
            scorer_payload = None
        if not scorer_integrity:
            scorer_payload = {
                "status": "failed",
                "reason": "trusted evaluation assets changed during scoring",
                "returncode": scorer_returncode,
            }
        elif scorer_payload is None:
            scorer_payload = {
                "status": "failed",
                "reason": "scorer did not emit a JSON object",
                "returncode": scorer_returncode,
            }
        scorer_result_path.write_text(
            json.dumps(scorer_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        record["scorer_status"] = (
            "passed"
            if scorer_integrity and scorer_returncode == 0 and scorer_payload.get("passed") is True
            else "failed"
        )
        record["scorer_returncode"] = scorer_returncode
        record["score"] = scorer_payload.get("score")
        record["passed"] = record["scorer_status"] == "passed"
        record["scorer_stdout_log"] = str(logs_dir / "scorer.stdout.log")
        record["scorer_stderr_log"] = str(logs_dir / "scorer.stderr.log")
        record["status"] = "passed" if record["scorer_status"] == "passed" else "failed"
        if scorer_timed_out:
            record["scorer_timeout_seconds"] = profile.get("timeout_seconds")
        print(
            f"[veriforge] roll {index}/{args.rolls}: score={record.get('score')} status={record['status']}",
            flush=True,
        )
    else:
        step(5, "skip scorer because Agent failed or integrity check failed")
    step(6, f"finish roll: {record.get('status', 'failed')}")
    remove_evaluation_source(evaluation_dir)
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
    parser.add_argument(
        "--developer-mode",
        action="store_true",
        help="enable local adapter/scorer/workspace overrides for development only",
    )
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
        help="developer-only scorer command; it runs after a successful agent",
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
    if not args.developer_mode and any(
        value is not None for value in (args.agent_command, args.scorer_command, args.workspace_source)
    ):
        raise ValueError("--agent-command, --scorer-command, and --workspace-source require --developer-mode")
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
        if not args.developer_mode:
            raise ValueError(f"harnesses file not found: {harness_path}")
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
    models = resolve_models(catalog, args)
    chat_adapter_run = len(models) == 1 and models[0].get("adapter") == "openai_chat"
    if len(harnesses) == 1 and not args.dry_run and not chat_adapter_run:
        executable = find_harness_executable(harness)
        if not executable:
            variable = harness.get("executable_env", "")
            raise ValueError(
                f"{harness.get('display_name', harness['id'])} CLI not found; "
                f"install {harness.get('executable')} or set {variable}"
            )
        if harness.get("executable_env"):
            os.environ[harness["executable_env"]] = executable
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
        participant_mode=not args.developer_mode,
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

    # Freeze the validated package before collecting a credential or starting
    # any roll. This prevents an editor/IDE save during a long run from
    # changing the source hash and invalidating an otherwise usable score.
    source_snapshot = None
    if not args.dry_run:
        source_root = args.workspace_source
        source_snapshot = create_source_snapshot(source_root)
        try:
            relative_task = args.task_spec.relative_to(source_root)
        except ValueError:
            relative_task = Path("01-task/task.yaml")
        args.workspace_source = source_snapshot
        args.task_spec = source_snapshot / relative_task
        atexit.register(remove_source_snapshot, source_snapshot)

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
    args.participant_mode = not args.developer_mode
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
    if source_snapshot is not None:
        remove_source_snapshot(source_snapshot)
    return 0 if all(record["status"] in {"dry_run", "passed"} for record in records) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
