#!/usr/bin/env python3
"""Regression tests for the participant-facing canonical model matrix."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "templates" / "runner" / "run_task.py"
WRAPPER_PATH = ROOT / "templates" / "runner" / "run_benchmark.sh"
MATRIX_PATH = ROOT / "examples" / "activity-models.yaml"
HARNESSES_PATH = ROOT / "templates" / "runner" / "harnesses.yaml"
CODEX_AGENT_PATH = ROOT / "templates" / "runner" / "codex_agent.sh"
PROVIDER_AGENT_PATH = ROOT / "templates" / "runner" / "provider_agent.py"


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

    def test_canonical_matrix_uses_the_approved_mixed_providers(self):
        models = {model["id"]: model for model in self.matrix["models"]}
        self.assertEqual(models["claude-opus-5"]["provider"], "wodex")
        self.assertEqual(models["gpt-5.6-sol"]["provider"], "wodex")
        self.assertEqual(models["qwen3.8-max"]["provider"], "aliyun_maas")
        self.assertEqual(models["kimi-k3"]["provider"], "moonshot")
        self.assertEqual(models["deepseek-v4-pro"]["provider"], "deepseek")

    def test_canonical_matrix_declares_trace_capabilities(self):
        models = {model["id"]: model for model in self.matrix["models"]}
        # 五个模型全部通过 CC/Codex CLI 运行，因此 trace 模式统一是
        # native_cli_stream；chat_tool_loop 保留给不经 CLI 的遗留直连 adapter。
        for model_id, model in models.items():
            with self.subTest(model=model_id):
                self.assertEqual(model["trace"]["mode"], "native_cli_stream")
                self.assertTrue(model["trace"]["streaming"])

    def test_mixed_provider_harness_matrix_is_accepted(self):
        catalog = RUNNER.load_harness_catalog(HARNESSES_PATH)
        self.assertEqual({entry["id"] for entry in catalog["harnesses"]}, {"cc", "codex"})
        self.assertEqual(catalog["credential_mode"], "per_selected_model")

    def test_harness_filters_model_choices_without_changing_selection_flow(self):
        args = type("Args", (), {"harness_id": "cc", "preflight": True, "model": None, "interactive": False})()
        models = RUNNER.resolve_models(self.matrix, args)
        self.assertEqual(
            {model["id"] for model in models},
            {"claude-opus-5", "kimi-k3", "qwen3.8-max", "deepseek-v4-pro"},
        )
        args.harness_id = "codex"
        models = RUNNER.resolve_models(self.matrix, args)
        self.assertEqual({model["id"] for model in models}, {"gpt-5.6-sol"})

    def test_every_model_protocol_matches_its_harness(self):
        """Codex 只接受 Responses 协议，CC 只接受 Anthropic 协议。
        协议与 harness 不匹配的模型在运行时必然失败（404 或握手错误），
        因此矩阵层面就要保证一致。"""
        required_adapter = {"cc": "anthropic_messages", "codex": "openai_responses"}
        for model in self.matrix["models"]:
            for harness_id in model["supported_harnesses"]:
                with self.subTest(model=model["id"], harness=harness_id):
                    self.assertEqual(model["adapter"], required_adapter[harness_id])

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
            shutil.copy2(HARNESSES_PATH, runner_dir / "harnesses.yaml")
            completed = subprocess.run(
                [str(runner_dir / "run_benchmark.sh"), "0"],
                cwd=Path(temp),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("1. 选择 harness", completed.stdout)
        self.assertIn("--rolls 必须在", completed.stderr)

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
        non_finite_timeout = copy.deepcopy(self.matrix)
        non_finite_timeout["models"][0]["profiles"][0]["timeout_seconds"] = float("nan")
        mutations.append(non_finite_timeout)
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
            env["CODEX_BIN"] = sys.executable
            base = [
                sys.executable,
                str(RUNNER_PATH),
                "--preflight",
                "--developer-mode",
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
        self.assertEqual(child_env["VERIFORGE_API_KEY"], "runtime-secret")
        self.assertEqual(
            json.loads(child_env["VERIFORGE_NATIVE_PARAMETERS_JSON"]),
            {"reasoning": {"effort": "xhigh"}, "max_output_tokens": 32768},
        )
        self.assertEqual(
            {key for key in child_env if key.endswith("_API_KEY")},
            {"WODEX_API_KEY", "VERIFORGE_API_KEY"},
        )

        kimi = next(model for model in self.matrix["models"] if model["id"] == "kimi-k3")
        kimi_env = RUNNER.build_environment(kimi, kimi["profiles"][0], "moonshot-secret", Path("task.yaml"))
        self.assertEqual(kimi_env["MOONSHOT_API_KEY"], "moonshot-secret")
        self.assertEqual(kimi_env["VERIFORGE_API_KEY"], "moonshot-secret")
        self.assertNotIn("WODEX_API_KEY", kimi_env)
        self.assertNotIn("DASHSCOPE_API_KEY", kimi_env)
        self.assertNotIn("DEEPSEEK_API_KEY", kimi_env)

    def test_codex_harness_exports_codex_api_key(self):
        script = CODEX_AGENT_PATH.read_text(encoding="utf-8")
        self.assertIn('export CODEX_API_KEY="$VERIFORGE_API_KEY"', script)
        self.assertIn('export OPENAI_API_KEY="$VERIFORGE_API_KEY"', script)
        self.assertIn("--json \\", script)

    def test_cc_harness_uses_streaming_output(self):
        script = (ROOT / "templates" / "runner" / "cc_agent.sh").read_text(encoding="utf-8")
        self.assertIn("--output-format stream-json", script)

    def test_each_model_uses_its_own_highest_effort(self):
        """推理档按模型固定：gpt-5.6-sol 的最高档是 xhigh，其余是 max。
        向 provider 下发它不支持的档位会被拒绝或静默降级。"""
        expected = {
            "kimi-k3": "max",
            "deepseek-v4-pro": "max",
            "qwen3.8-max": "max",
            "claude-opus-5": "max",
            "gpt-5.6-sol": "xhigh",
        }
        self.assertEqual(RUNNER.FIXED_REASONING_EFFORT, expected)
        for model in self.matrix["models"]:
            with self.subTest(model=model["id"]):
                parameters = model["profiles"][0]["parameters"]
                self.assertEqual(parameters["reasoning_effort"], expected[model["id"]])
                self.assertEqual(parameters["max_output_tokens"], RUNNER.FIXED_MAX_OUTPUT_TOKENS)

    def test_choose_rolls_accepts_default_and_rejects_out_of_range(self):
        """rollout 次数交互选择：回车用默认、越界重问、非法输入重问。"""
        import builtins
        from unittest import mock

        cases = [
            (["\n"], 3),          # 直接回车 -> 默认
            (["5"], 5),           # 合法值
            (["99", "2"], 2),     # 越界后重输
            (["abc", "1"], 1),    # 非法输入后重输
            (["0", "10"], 10),    # 低于下界后重输
        ]
        for answers, expected in cases:
            with self.subTest(answers=answers):
                with mock.patch.object(builtins, "input", side_effect=answers):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(RUNNER.choose_rolls(1, 10, 3), expected)

    def test_wrapper_defers_rollout_choice_to_interactive_flow(self):
        """包装脚本不再写死 3 次；无参数时把次数交给交互流程。"""
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertIn("3. 选择 rollout 次数", wrapper)
        self.assertIn("exec python3 03-runner/run_task.py --interactive\n", wrapper)
        runner_src = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("rolls_from_cli", runner_src)

    def test_output_symlinks_are_rejected(self):
        """outputs/ 属于 mutable_paths，被完整性哈希跳过，因此符号链接不受约束。
        Agent 可以把 result.json 软链到 workspace 外，让 scorer 读到不受约束的
        内容并拿到分数——必须在评分前拒绝。"""
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp) / "outputs"
            (output_dir / "nested").mkdir(parents=True)
            (output_dir / "result.json").write_text("{}", encoding="utf-8")
            self.assertEqual(RUNNER.find_output_symlinks(output_dir), [])

            outside = Path(temp) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            (output_dir / "leak.json").symlink_to(outside)
            (output_dir / "nested" / "deep.json").symlink_to(outside)
            self.assertEqual(
                RUNNER.find_output_symlinks(output_dir),
                ["leak.json", "nested/deep.json"],
            )

    def test_cc_workspace_settings_deny_absolute_paths(self):
        """CC 的项目级 deny 无法被 allow 覆盖，用它锁死绝对路径读写。"""
        deny = RUNNER.CC_WORKSPACE_SETTINGS["permissions"]["deny"]
        self.assertEqual(sorted(deny), ["Edit(//**)", "Read(//**)", "Write(//**)"])
        runner_src = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn('workspace / ".claude" / "settings.json"', runner_src)

    def test_codex_config_pins_sandbox_and_approval(self):
        """approval_policy = never 是唯一不依赖 Landlock 的一项：
        自动批准会把沙箱拦下的命令放行到沙箱外重试，必须断掉。"""
        codex = CODEX_AGENT_PATH.read_text(encoding="utf-8")
        self.assertIn('sandbox_mode = "workspace-write"', codex)
        self.assertIn('approval_policy = "never"', codex)
        self.assertIn("writable_roots = []", codex)
        self.assertIn("exclude_slash_tmp = true", codex)
        self.assertIn("exclude_tmpdir_env_var = true", codex)
        self.assertIn("network_access = false", codex)
        # 命令行 --approve-for-me 优先级高于 config.toml，会覆盖 approval_policy
        self.assertNotIn("--approve-for-me", codex)

    def test_both_harnesses_pin_one_million_context(self):
        """上下文窗口统一 1M 且必须显式下发。
        CC 对无法识别的模型默认只按 200K 计算，不显式声明会让不带 [1m] 后缀的
        模型被砍到实际能力的 1/5，跨模型分数不可比。"""
        self.assertEqual(RUNNER.FIXED_CONTEXT_WINDOW, 1_000_000)
        self.assertEqual(RUNNER.FIXED_AUTO_COMPACT_LIMIT, 900_000)

        codex = CODEX_AGENT_PATH.read_text(encoding="utf-8")
        self.assertIn("model_context_window = $context_window", codex)
        self.assertIn("model_auto_compact_token_limit = $auto_compact_limit", codex)
        # 重试显式写死，不依赖会随版本变化的 CLI 默认值
        self.assertIn("request_max_retries = 4", codex)
        self.assertIn("stream_max_retries = 5", codex)

        cc = (ROOT / "templates" / "runner" / "cc_agent.sh").read_text(encoding="utf-8")
        self.assertIn("export CLAUDE_CODE_MAX_CONTEXT_TOKENS", cc)
        self.assertIn("export CLAUDE_CODE_AUTO_COMPACT_WINDOW", cc)

        env = RUNNER.build_environment(
            self.matrix["models"][0], self.matrix["models"][0]["profiles"][0], None, Path("task.yaml")
        )
        self.assertEqual(env["VERIFORGE_CONTEXT_WINDOW"], "1000000")
        self.assertEqual(env["VERIFORGE_AUTO_COMPACT_LIMIT"], "900000")

    def test_agent_and_scorer_timeouts_are_independent(self):
        """Agent 超时 2 小时，scorer 用独立短超时。
        若 scorer 复用 Agent 超时，一个死循环的 scorer 会拖满 2 小时才被发现。"""
        for model in self.matrix["models"]:
            with self.subTest(model=model["id"]):
                self.assertEqual(model["profiles"][0]["timeout_seconds"], 7200)
        self.assertEqual(RUNNER.SCORER_TIMEOUT_SECONDS, 300)
        runner_src = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("timeout=SCORER_TIMEOUT_SECONDS", runner_src)

    def test_harness_adapters_read_effort_from_profile(self):
        """推理档必须来自 profile，不能硬编码。
        写死档位会让 models.yaml 的固定参数失效，实际运行档位与矩阵声明不一致，
        跨模型分数因此不可比。"""
        codex = CODEX_AGENT_PATH.read_text(encoding="utf-8")
        self.assertIn("VERIFORGE_NATIVE_PARAMETERS_JSON", codex)
        self.assertIn('model_reasoning_effort = "$reasoning_effort"', codex)
        self.assertNotIn('model_reasoning_effort = "xhigh"', codex)

        self.assertIn("model_max_output_tokens = $max_output_tokens", codex)

        cc = (ROOT / "templates" / "runner" / "cc_agent.sh").read_text(encoding="utf-8")
        self.assertIn("VERIFORGE_NATIVE_PARAMETERS_JSON", cc)
        self.assertIn("export CLAUDE_CODE_EFFORT_LEVEL", cc)
        self.assertIn("export CLAUDE_CODE_MAX_OUTPUT_TOKENS", cc)

    def test_provider_adapters_translate_canonical_parameters(self):
        expected = {
            "claude-opus-5": {"output_config": {"effort": "max"}, "max_tokens": 32768},
            # gpt-5.6-sol 的最高档是 xhigh，不是 max
            "gpt-5.6-sol": {"reasoning": {"effort": "xhigh"}, "max_output_tokens": 32768},
            "qwen3.8-max": {"output_config": {"effort": "max"}, "max_tokens": 32768},
            "kimi-k3": {"output_config": {"effort": "max"}, "max_tokens": 32768},
            "deepseek-v4-pro": {"output_config": {"effort": "max"}, "max_tokens": 32768},
        }
        for model in self.matrix["models"]:
            with self.subTest(model=model["id"]):
                self.assertEqual(
                    RUNNER.build_provider_parameters(model, model["profiles"][0]),
                    expected[model["id"]],
                )

    def test_chat_adapter_is_shipped_and_validates_missing_response_fields(self):
        self.assertTrue(PROVIDER_AGENT_PATH.is_file())
        source = PROVIDER_AGENT_PATH.read_text(encoding="utf-8")
        self.assertIn("response_format", source)
        self.assertIn("choices[0].message.content", source)
        self.assertIn("MAX_PROVIDER_ATTEMPTS", source)
        module_spec = importlib.util.spec_from_file_location("provider_adapter", PROVIDER_AGENT_PATH)
        self.assertIsNotNone(module_spec)
        module = importlib.util.module_from_spec(module_spec)
        assert module_spec.loader is not None
        module_spec.loader.exec_module(module)
        with self.assertRaises(module.ProviderRequestError):
            module.response_text("openai_chat", {})

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
                "output=Path(os.environ['VERIFORGE_OUTPUT_DIR']); output.mkdir(parents=True, exist_ok=True); "
                "marker=output / 'marker.txt'; seen=marker.exists(); "
                "print(f'marker-existed={seen} key={os.environ.get(\"WODEX_API_KEY\")}'); "
                "marker.write_text('created', encoding='utf-8')\n",
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
            self.assertEqual(Path(first["output_dir"]).joinpath("marker.txt").read_text(), "created")
            self.assertEqual(Path(second["output_dir"]).joinpath("marker.txt").read_text(), "created")
            self.assertIn("marker-existed=False", Path(second["agent_stdout_log"]).read_text())
            self.assertNotIn("runtime-secret", Path(second["agent_stdout_log"]).read_text())
            self.assertIn("<redacted>", Path(second["agent_stdout_log"]).read_text())
            staged_spec = Path(second["task_spec"])
            self.assertTrue(staged_spec.exists())
            self.assertEqual(staged_spec.stat().st_mode & stat.S_IWUSR, 0)
            self.assertTrue(Path(first["agent_stderr_log"]).exists())
            self.assertEqual(json.loads(Path(second["scorer_result"]).read_text())["passed"], True)
            self.assertEqual(second["score"], 100)
            self.assertFalse(Path(second["workspace"], "02-evaluation", "scorer.py").exists())
            self.assertIn("scorer_hash", second)

    def test_evaluation_assets_are_hidden_from_agent_and_scorer_is_trusted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation" / "reference_answer").mkdir(parents=True)
            (source / "02-evaluation" / "validators").mkdir(parents=True)
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json; print(json.dumps({'score': 100, 'passed': True}))\n",
                encoding="utf-8",
            )
            (source / "02-evaluation" / "rubric.yaml").write_text("max_score: 100\n", encoding="utf-8")
            (source / "02-evaluation" / "reference_answer" / "expected.json").write_text("{}\n", encoding="utf-8")
            (source / "02-evaluation" / "validators" / "check.py").write_text("# trusted\n", encoding="utf-8")
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text(
                "from pathlib import Path; "
                "Path('outputs/asset-visible.txt').write_text(str(Path('02-evaluation/scorer.py').exists()), encoding='utf-8')\n",
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
                    "task_id": "trusted-evaluation-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertEqual(Path(record["output_dir"], "asset-visible.txt").read_text(), "False")
            self.assertEqual(record["scorer_status"], "passed")
            self.assertEqual(record["score"], 100)
            self.assertEqual(record["scorer_hash"], record["integrity_hashes"]["scorer"])

    def test_writes_outside_mutable_paths_fail_before_scoring(self):
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
                "from pathlib import Path; Path('forbidden.txt').write_text('changed', encoding='utf-8')\n",
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
                    "task_id": "mutable-boundary-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertFalse(record["workspace_integrity"])
            self.assertEqual(record["scorer_status"], "skipped_integrity_failure")
            self.assertFalse((Path(record["scorer_result"]).read_text()).find('"passed": true') >= 0)

    def test_agent_cannot_inject_workspace_scorer_for_a_full_score(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json; print(json.dumps({'score': 0, 'passed': False}))\n",
                encoding="utf-8",
            )
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text(
                "from pathlib import Path; "
                "p=Path('02-evaluation/scorer.py'); p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text(\"import json; print(json.dumps({'score': 100, 'passed': True}))\\n\", encoding='utf-8')\n",
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
                    "task_id": "workspace-scorer-injection-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertFalse(record["workspace_integrity"])
            self.assertEqual(record["scorer_status"], "skipped_integrity_failure")
            self.assertNotEqual(record.get("score"), 100)
            self.assertNotEqual(record.get("passed"), True)

    def test_agent_cannot_prewrite_scorer_result_for_a_full_score(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json; print(json.dumps({'score': 0, 'passed': False}))\n",
                encoding="utf-8",
            )
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text(
                "import json, os; from pathlib import Path; "
                "Path(os.environ['VERIFORGE_ROLL_DIR'], 'scorer-result.json').write_text("
                "json.dumps({'score': 100, 'passed': True}), encoding='utf-8')\n",
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
                    "task_id": "scorer-result-injection-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertEqual(record["scorer_status"], "failed")
            self.assertEqual(record["score"], 0)
            self.assertFalse(record["passed"])

    def test_scorer_stdout_is_authoritative_over_result_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json, os; from pathlib import Path; "
                "output=Path(os.environ['VERIFORGE_OUTPUT_DIR']); "
                "Path(output.parents[1], 'scorer-result.json').write_text("
                "json.dumps({'score': 100, 'passed': True}), encoding='utf-8'); "
                "print(json.dumps({'score': 0, 'passed': False}))\n",
                encoding="utf-8",
            )
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text("\n", encoding="utf-8")
            task_spec = source / "01-task" / "task.yaml"
            task_spec.write_text("status: verified\n", encoding="utf-8")
            selected = next(model for model in self.matrix["models"] if model["id"] == "gpt-5.6-sol")
            args = type(
                "Args",
                (),
                {
                    "agent_command": None,
                    "dry_run": False,
                    "task_id": "authoritative-stdout-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertEqual(record["score"], 0)
            self.assertFalse(record["passed"])
            self.assertEqual(json.loads(Path(record["scorer_result"]).read_text())["score"], 0)

    def test_scorer_copy_change_during_scoring_invalidates_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json; from pathlib import Path; p=Path(__file__); p.chmod(0o700); "
                "p.write_text(p.read_text(encoding='utf-8') + '# changed\\n', encoding='utf-8'); "
                "print(json.dumps({'score': 100, 'passed': True}))\n",
                encoding="utf-8",
            )
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text("\n", encoding="utf-8")
            task_spec = source / "01-task" / "task.yaml"
            task_spec.write_text("status: verified\n", encoding="utf-8")
            selected = next(model for model in self.matrix["models"] if model["id"] == "gpt-5.6-sol")
            args = type(
                "Args",
                (),
                {
                    "agent_command": None,
                    "dry_run": False,
                    "task_id": "scorer-integrity-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertFalse(record["scorer_integrity"])
            self.assertEqual(record["scorer_status"], "failed")
            self.assertNotEqual(record.get("score"), 100)
            self.assertFalse(record["passed"])

    def test_agent_cannot_modify_trusted_evaluation_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation").mkdir()
            (source / "02-evaluation" / "scorer.py").write_text(
                "import json; print(json.dumps({'score': 0, 'passed': False}))\n",
                encoding="utf-8",
            )
            (source / "03-runner").mkdir()
            (source / "03-runner" / "provider_agent.py").write_text(
                "import os; from pathlib import Path; "
                "temp_root=Path(os.environ['VERIFORGE_ROLL_DIR']).parents[2]; "
                "paths=list(temp_root.glob('veriforge-evaluation-*')); "
                "assert paths; p=max(paths, key=lambda item: item.stat().st_mtime) / 'scorer.py'; "
                "p.chmod(0o700); p.write_text("
                "\"import json; print(json.dumps({'score': 100, 'passed': True}))\\n\", encoding='utf-8')\n",
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
                    "task_id": "evaluation-copy-injection-test",
                    "rolls": 1,
                    "task_spec": task_spec,
                    "workspace_source": source,
                    "results_dir": root / "results",
                    "scorer_command": None,
                },
            )()
            record = RUNNER.run_roll(selected, selected["profiles"][0], "runtime-secret", args, 1, "catalog", "task")
            self.assertFalse(record["evaluation_integrity"])
            self.assertEqual(record["scorer_status"], "skipped_integrity_failure")
            self.assertNotEqual(record.get("score"), 100)

    def test_integrity_hashes_cover_all_benchmark_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "package"
            (source / "01-task").mkdir(parents=True)
            (source / "02-evaluation" / "reference_answer").mkdir(parents=True)
            (source / "02-evaluation" / "validators").mkdir(parents=True)
            (source / "03-runner").mkdir(parents=True)
            for relative in (
                "02-evaluation/scorer.py",
                "02-evaluation/rubric.yaml",
                "02-evaluation/reference_answer/expected.json",
                "02-evaluation/validators/check.py",
                "03-runner/harnesses.yaml",
                "03-runner/models.yaml",
                "03-runner/harness.allowlist.yaml",
                "03-runner/dependency-manifest.yaml",
                "03-runner/isolation-manifest.yaml",
                "03-runner/cc_agent.sh",
                "03-runner/codex_agent.sh",
                "03-runner/provider_agent.py",
                "02-evaluation/fixtures/input.txt",
            ):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
            task_spec = source / "01-task" / "task.yaml"
            task_spec.write_text("status: verified\n", encoding="utf-8")
            manifest = {"fixture_paths": ["02-evaluation/fixtures"]}
            before = RUNNER.package_integrity_hashes(source, task_spec, manifest, required=True)
            self.assertTrue(
                {
                    "scorer",
                    "rubric",
                    "reference_answer",
                    "validators",
                    "harnesses",
                    "models",
                    "allowlist",
                    "dependency_manifest",
                    "isolation_manifest",
                    "task_spec",
                }.issubset(before)
            )
            self.assertTrue(any(key.startswith("fixture:") for key in before))
            (source / "03-runner" / "harness.allowlist.yaml").write_text("changed", encoding="utf-8")
            after = RUNNER.package_integrity_hashes(source, task_spec, manifest, required=True)
            self.assertNotEqual(before["allowlist"], after["allowlist"])

    def test_participant_mode_rejects_developer_overrides(self):
        original_argv = sys.argv
        try:
            overrides = (
                ["--workspace-source", "/tmp/untrusted-package"],
                ["--scorer-command", "untrusted-scorer"],
                ["--agent-command", "untrusted-agent"],
            )
            for override in overrides:
                with self.subTest(override=override[0]):
                    sys.argv = [str(RUNNER_PATH), *override]
                    with self.assertRaisesRegex(ValueError, "需要 --developer-mode"):
                        RUNNER.main()
        finally:
            sys.argv = original_argv

    def test_harnesses_hide_runner_internal_paths_from_model_process(self):
        for path in (ROOT / "templates" / "runner" / "cc_agent.sh", CODEX_AGENT_PATH):
            script = path.read_text(encoding="utf-8")
            self.assertIn("unset VERIFORGE_ROLL_DIR VERIFORGE_SCORER_RESULT", script)

    def test_timeout_escalates_to_sigkill(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "ignore_term.py"
            marker = Path(temp) / "term-received.txt"
            script.write_text(
                "import signal, time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, lambda *_: Path('term-received.txt').write_text('yes')); "
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            returncode, _stdout, _stderr, timed_out = RUNNER.run_command(
                [sys.executable, str(script)],
                cwd=Path(temp),
                env={"PATH": os.environ.get("PATH", "")},
                timeout=0.05,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(returncode, 124)
            self.assertTrue(timed_out)
            self.assertEqual(marker.read_text(), "yes")
            self.assertLess(elapsed, 3)

    def test_run_command_streams_redacted_progress_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "stream.py"
            script.write_text(
                "import json, sys; "
                "print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'echo secret-token'}}), flush=True); "
                "print('secret-token', file=sys.stderr, flush=True)\n",
                encoding="utf-8",
            )
            terminal = io.StringIO()
            with redirect_stdout(terminal):
                returncode, stdout, stderr, timed_out = RUNNER.run_command(
                    [sys.executable, str(script)],
                    cwd=Path(temp),
                    env={"PATH": os.environ.get("PATH", "")},
                    timeout=2,
                    live_label="roll 1 agent",
                    credential="secret-token",
                    heartbeat_seconds=0.1,
                )
            self.assertEqual(returncode, 0)
            self.assertFalse(timed_out)
            self.assertIn("secret-token", stdout)
            self.assertIn("secret-token", stderr)
            self.assertIn("[veriforge][roll 1 agent][stdout]", terminal.getvalue())
            self.assertIn("item.started command_execution", terminal.getvalue())
            self.assertIn("<redacted>", terminal.getvalue())
            self.assertNotIn("secret-token", terminal.getvalue())

    def test_trace_bundle_is_secret_redacted_and_indexed(self):
        with tempfile.TemporaryDirectory() as temp:
            trace_dir = Path(temp) / "trace"
            paths = RUNNER.write_trace_bundle(
                trace_dir,
                stdout='{"type":"tool_call","input":"secret-token"}\n',
                stderr='provider request secret-token\n',
                credential="secret-token",
                mode="native_cli_stream",
                streaming=True,
                tool_calls=True,
                normalized_events=True,
            )
            index = json.loads(Path(paths["trace_index"]).read_text(encoding="utf-8"))
            events = Path(paths["trace_events"]).read_text(encoding="utf-8")
            self.assertEqual(index["trace_version"], "veriforge-trace/v1")
            self.assertTrue(index["tool_calls_available"])
            self.assertGreaterEqual(index["event_count"], 2)
            self.assertNotIn("secret-token", events)
            self.assertIn("<redacted>", events)

    def test_chat_tool_loop_tools_are_path_restricted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixtures = root / "fixtures"
            outputs = root / "outputs"
            fixtures.mkdir()
            outputs.mkdir()
            fixtures.joinpath("profile.json").write_text("{}\n", encoding="utf-8")
            module_spec = importlib.util.spec_from_file_location("provider_trace_test", PROVIDER_AGENT_PATH)
            module = importlib.util.module_from_spec(module_spec)
            assert module_spec.loader is not None
            module_spec.loader.exec_module(module)
            self.assertEqual(module.execute_tool("list_fixtures", {}, fixtures, outputs), '["profile.json"]')
            with self.assertRaises(ValueError):
                module.execute_tool("read_fixture", {"path": "../outside"}, fixtures, outputs)
            with self.assertRaises(ValueError):
                module.execute_tool("unknown", {}, fixtures, outputs)

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
                "--developer-mode",
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
