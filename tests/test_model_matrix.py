#!/usr/bin/env python3
"""Regression tests for the participant-facing canonical model matrix."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "templates" / "runner" / "run_task.py"
WRAPPER_PATH = ROOT / "templates" / "runner" / "run_benchmark.sh"
MATRIX_PATH = ROOT / "examples" / "activity-models.yaml"
HARNESSES_PATH = ROOT / "templates" / "runner" / "harnesses.yaml"


def load_runner():
    spec = importlib.util.spec_from_file_location("veriforge_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load runner module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = load_runner()


class CanonicalMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))

    def write_matrix(self, directory: Path, matrix: dict) -> Path:
        path = directory / "models.yaml"
        path.write_text(yaml.safe_dump(matrix, sort_keys=False), encoding="utf-8")
        return path

    def test_canonical_matrix_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_matrix(Path(temp), self.matrix)
            loaded = RUNNER.load_catalog(path)
        self.assertEqual({model["id"] for model in loaded["models"]}, RUNNER.CANONICAL_MODEL_IDS)

    def test_wodex_harness_matrix_is_accepted(self):
        catalog = RUNNER.load_harness_catalog(HARNESSES_PATH)
        self.assertEqual({entry["id"] for entry in catalog["harnesses"]}, {"cc", "codex"})
        self.assertEqual(catalog["credential_env"], "WODEX_API_KEY")

    def test_harness_selection_never_prefers_provider_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "package"
            (package / "03-runner").mkdir(parents=True)
            (package / "03-runner" / "provider_agent.py").write_text("", encoding="utf-8")
            (package / "03-runner" / "cc_agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (package / "03-runner" / "codex_agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertEqual(
                RUNNER.resolve_agent_command(package, None, "cc"),
                [str(package / "03-runner" / "cc_agent.sh")],
            )
            self.assertEqual(
                RUNNER.resolve_agent_command(package, None, "codex"),
                [str(package / "03-runner" / "codex_agent.sh")],
            )

    def test_participant_wrapper_is_executable_and_cwd_independent(self):
        self.assertTrue(os.access(WRAPPER_PATH, os.X_OK))
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "benchmark"
            runner_dir = package / "03-runner"
            runner_dir.mkdir(parents=True)
            shutil.copy2(WRAPPER_PATH, runner_dir / "run_benchmark.sh")
            shutil.copy2(RUNNER_PATH, runner_dir / "run_task.py")
            shutil.copy2(MATRIX_PATH, runner_dir / "models.yaml")
            completed = subprocess.run(
                [str(runner_dir / "run_benchmark.sh"), "0"],
                cwd=Path(temp),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("1. 选择 harness", completed.stdout)
        self.assertIn("--rolls must be between", completed.stderr)

    def test_default_results_directory_names_model_and_timestamp(self):
        result_path = RUNNER.default_results_dir("gpt-5.6-sol")
        self.assertEqual(result_path.parent, Path("results"))
        self.assertRegex(
            result_path.name,
            r"^gpt-5\.6-sol-\d{8}T\d{12}Z$",
        )

    def test_missing_or_extra_model_is_rejected(self):
        for mutation in ("missing", "extra"):
            matrix = copy.deepcopy(self.matrix)
            if mutation == "missing":
                matrix["models"] = matrix["models"][:-1]
                matrix["organizer_controls"]["approved_model_ids"] = matrix["organizer_controls"]["approved_model_ids"][:-1]
            else:
                extra = copy.deepcopy(matrix["models"][0])
                extra["id"] = "unapproved-model"
                matrix["models"].append(extra)
                matrix["organizer_controls"]["approved_model_ids"].append("unapproved-model")
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    RUNNER.load_catalog(self.write_matrix(Path(temp), matrix))

    def test_unknown_id_and_noncanonical_mapping_are_rejected(self):
        mutations = []
        unknown = copy.deepcopy(self.matrix)
        unknown["models"][0]["id"] = "unapproved-model"
        mutations.append(unknown)
        wrong_mapping = copy.deepcopy(self.matrix)
        wrong_mapping["models"][0]["credential_env"] = "WRONG_API_KEY"
        mutations.append(wrong_mapping)
        for matrix in mutations:
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    RUNNER.load_catalog(self.write_matrix(Path(temp), matrix))

    def test_second_profile_non_default_profile_and_parameter_override_are_rejected(self):
        mutations = []
        extra_profile = copy.deepcopy(self.matrix)
        extra_profile["models"][0]["profiles"].append(copy.deepcopy(extra_profile["models"][0]["profiles"][0]))
        mutations.append(extra_profile)
        non_default = copy.deepcopy(self.matrix)
        non_default["models"][0]["profiles"][0]["id"] = "fast"
        mutations.append(non_default)
        changed_parameters = copy.deepcopy(self.matrix)
        changed_parameters["models"][0]["profiles"][0]["parameters"]["max_output_tokens"] = 1234
        mutations.append(changed_parameters)
        for matrix in mutations:
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    RUNNER.load_catalog(self.write_matrix(Path(temp), matrix))

    def test_preflight_requires_only_selected_credential(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            matrix_path = self.write_matrix(directory, self.matrix)
            task_path = directory / "task.yaml"
            task_path.write_text(
                yaml.safe_dump({"schema_version": "veriforge-task/v1", "task_id": "example-activity-v1", "status": "verified"}),
                encoding="utf-8",
            )
            (directory / "03-runner").mkdir()
            (directory / "03-runner" / "provider_agent.py").write_text("", encoding="utf-8")
            (directory / "02-evaluation").mkdir()
            (directory / "02-evaluation" / "scorer.py").write_text("", encoding="utf-8")
            env = os.environ.copy()
            for variable in (model["credential_env"] for model in self.matrix["models"]):
                env.pop(variable, None)
            base = [
                sys.executable,
                str(RUNNER_PATH),
                "--preflight",
                "--models-file",
                str(matrix_path),
                "--task-spec",
                str(task_path),
                "--task-id",
                "example-activity-v1",
            ]
            matrix_only = subprocess.run(base, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(matrix_only.returncode, 0, matrix_only.stderr)
            selected = subprocess.run(base + ["--model", "gpt-5.6-sol"], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(selected.returncode, 2, selected.stdout + selected.stderr)

    def test_child_environment_contains_only_selected_credential(self):
        selected = next(model for model in self.matrix["models"] if model["id"] == "gpt-5.6-sol")
        profile = selected["profiles"][0]
        child_env = RUNNER.build_environment(selected, profile, "runtime-secret", Path("task.yaml"))
        self.assertEqual(child_env["WODEX_API_KEY"], "runtime-secret")
        self.assertEqual(
            json.loads(child_env["VERIFORGE_NATIVE_PARAMETERS_JSON"]),
            {"reasoning": {"effort": "max"}, "max_output_tokens": 32768},
        )
        self.assertEqual(
            {key for key in child_env if key.endswith("_API_KEY")},
            {"WODEX_API_KEY"},
        )

    def test_provider_adapters_translate_canonical_parameters(self):
        expected = {
            "claude-opus-5": {"output_config": {"effort": "max"}, "max_tokens": 32768},
            "gpt-5.6-sol": {"reasoning": {"effort": "max"}, "max_output_tokens": 32768},
            "qwen3.8-max": {"reasoning_effort": "max", "max_tokens": 32768},
            "kimi-k3": {"reasoning_effort": "max", "max_tokens": 32768},
            "deepseek-v4-pro": {"reasoning_effort": "max", "max_tokens": 32768},
        }
        for model in self.matrix["models"]:
            with self.subTest(model=model["id"]):
                self.assertEqual(
                    RUNNER.build_provider_parameters(model, model["profiles"][0]),
                    expected[model["id"]],
                )

    def test_rolls_have_isolated_workspaces_logs_and_read_only_spec(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json; print(json.dumps({'score': 100, 'passed': True}))\n",
                encoding="utf-8",
            )
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text(
                "import os; from pathlib import Path; "
                "seen=Path('marker.txt').exists(); "
                "print(f'marker-existed={seen} key={os.environ.get(\"WODEX_API_KEY\")}'); "
                "Path('marker.txt').write_text('created', encoding='utf-8')\n",
                encoding="utf-8",
            )
            task_spec = source / "01-task" / "task.yaml"
            task_spec.write_text("status: verified\n", encoding="utf-8")
            results = root / "results"
            selected = next(model for model in self.matrix["models"] if model["id"] == "gpt-5.6-sol")
            profile = selected["profiles"][0]
            args = type(
                "Args",
                (),
                {
                    "agent_command": None,
                    "dry_run": False,
                    "task_id": "isolation-test",
                    "rolls": 2,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": results,
                    "scorer_command": None,
                },
            )()
            first = RUNNER.run_roll(selected, profile, "runtime-secret", args, 1, "catalog", "task")
            second = RUNNER.run_roll(selected, profile, "runtime-secret", args, 2, "catalog", "task")

            self.assertNotEqual(first["workspace"], second["workspace"])
            self.assertEqual(Path(first["workspace"]).joinpath("marker.txt").read_text(), "created")
            self.assertEqual(Path(second["workspace"]).joinpath("marker.txt").read_text(), "created")
            self.assertIn("marker-existed=False", Path(second["agent_stdout_log"]).read_text())
            self.assertNotIn("runtime-secret", Path(second["agent_stdout_log"]).read_text())
            self.assertIn("<redacted>", Path(second["agent_stdout_log"]).read_text())
            staged_spec = Path(second["task_spec"])
            self.assertTrue(staged_spec.exists())
            self.assertEqual(staged_spec.stat().st_mode & stat.S_IWUSR, 0)
            self.assertTrue(Path(first["agent_stderr_log"]).exists())
            self.assertEqual(json.loads(Path(second["scorer_result"]).read_text())["passed"], True)
            self.assertEqual(second["score"], 100)

    def test_task_spec_modification_fails_before_scorer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json; print(json.dumps({'score': 100, 'passed': True}))\n",
                encoding="utf-8",
            )
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text(
                "import os; from pathlib import Path; "
                "p=Path(os.environ['VERIFORGE_TASK_SPEC']); "
                "p.chmod(0o600); p.write_text('status: verified\\nchanged: true\\n')\n",
                encoding="utf-8",
            )
            task_spec = source / "01-task" / "task.yaml"
            task_spec.write_text("status: verified\n", encoding="utf-8")
            selected = next(model for model in self.matrix["models"] if model["id"] == "gpt-5.6-sol")
            args = type(
                "Args",
                (),
                {
                    "agent_command": None,
                    "dry_run": False,
                    "task_id": "integrity-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertFalse(record["task_spec_integrity"])
            self.assertEqual(record["scorer_status"], "skipped_integrity_failure")
            self.assertEqual(record["status"], "failed")

    def test_main_writes_per_roll_manifest_from_clean_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text("", encoding="utf-8")
            shutil.copy2(HARNESSES_PATH, source / "03-runner" / "harnesses.yaml")
            shutil.copy2(ROOT / "templates" / "runner" / "codex_agent.sh", source / "03-runner" / "codex_agent.sh")
            shutil.copy2(ROOT / "templates" / "runner" / "cc_agent.sh", source / "03-runner" / "cc_agent.sh")
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text("", encoding="utf-8")
            task_spec = source / "01-task" / "task.yaml"
            task_spec.write_text("status: verified\n", encoding="utf-8")
            matrix_path = self.write_matrix(source / "03-runner", self.matrix)
            results = root / "results"
            original_argv = sys.argv
            try:
                sys.argv = [
                    str(RUNNER_PATH),
                "--harness",
                "codex",
                "--model",
                    "gpt-5.6-sol",
                    "--rolls",
                    "2",
                    "--dry-run",
                    "--models-file",
                    str(matrix_path),
                    "--task-spec",
                    str(task_spec),
                    "--workspace-source",
                    str(source),
                    "--results-dir",
                    str(results),
                ]
                self.assertEqual(RUNNER.main(), 0)
            finally:
                sys.argv = original_argv
            manifest = json.loads((results / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["runs"]), 2)
            self.assertNotEqual(manifest["runs"][0]["workspace"], manifest["runs"][1]["workspace"])
            self.assertEqual(manifest["runs"][0]["status"], "dry_run")
            self.assertEqual(
                json.loads(Path(manifest["runs"][0]["scorer_result"]).read_text())["status"],
                "not_run",
            )


if __name__ == "__main__":
    unittest.main()
