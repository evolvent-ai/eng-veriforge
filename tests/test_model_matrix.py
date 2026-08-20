#!/usr/bin/env python3
"""Regression tests for the participant-facing canonical model matrix."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "templates" / "runner" / "run_task.py"
MATRIX_PATH = ROOT / "examples" / "activity-models.yaml"


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
        self.assertEqual(child_env["OPENAI_API_KEY"], "runtime-secret")
        for model in self.matrix["models"]:
            if model is not selected:
                self.assertNotIn(model["credential_env"], child_env)


if __name__ == "__main__":
    unittest.main()
