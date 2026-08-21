#!/usr/bin/env python3
"""VeriForge 任务包脚手架。

从 templates/ 和 examples/ 拷齐一个完整的 benchmark 任务包骨架，
统一替换 task_id，并设置脚本可执行权限。

用法：
    python3 scripts/new_task.py <task-slug> [--output-dir DIR] [--title TITLE]

生成后仍需人工填写 01-task/task.yaml 的题干、02-evaluation/ 的判定逻辑，
以及两个 adapter prompt 中的输出契约。详见 references/task-contract.md。
"""

import argparse
import re
import shutil
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_TASK_ID = "example-task-v1"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# 原样复制的 runner 资产：(源路径, 包内目标路径)
RUNNER_ASSETS = [
    ("templates/runner/run_task.py", "03-runner/run_task.py"),
    ("templates/runner/run_benchmark.sh", "03-runner/run_benchmark.sh"),
    ("templates/runner/harnesses.yaml", "03-runner/harnesses.yaml"),
    ("templates/runner/cc_agent.sh", "03-runner/cc_agent.sh"),
    ("templates/runner/codex_agent.sh", "03-runner/codex_agent.sh"),
    ("templates/runner/provider_agent.py", "03-runner/provider_agent.py"),
    ("examples/activity-models.yaml", "03-runner/models.yaml"),
]

# 需要 chmod +x 的文件
EXECUTABLE_PATHS = [
    "03-runner/run_benchmark.sh",
    "03-runner/cc_agent.sh",
    "03-runner/codex_agent.sh",
    "03-runner/provider_agent.py",
    "02-evaluation/scorer.py",
]

# 生成后必须存在的完整清单，与 references/task-contract.md 的必需文件清单一致
REQUIRED_PATHS = [
    "01-task/task.yaml",
    "01-task/README.md",
    "02-evaluation/scorer.py",
    "02-evaluation/rubric.yaml",
    "02-evaluation/reference_answer",
    "02-evaluation/validators",
    "02-evaluation/fixtures",
    "03-runner/models.yaml",
    "03-runner/harnesses.yaml",
    "03-runner/harness.allowlist.yaml",
    "03-runner/dependency-manifest.yaml",
    "03-runner/isolation-manifest.yaml",
    "03-runner/run_task.py",
    "03-runner/run_benchmark.sh",
    "03-runner/cc_agent.sh",
    "03-runner/codex_agent.sh",
    "03-runner/provider_agent.py",
]

README_TEMPLATE = """# {title}

{objective}

## 快速开始

```bash
./03-runner/run_benchmark.sh
```

依次选择 harness（Claude Code 或 Codex）、选择一个模型、隐藏输入该模型的
API Key，然后自动执行 3 次隔离 rollout 并评分。每次运行使用新的
`results/<model>-<timestamp>/` 目录。

指定 rollout 次数：

```bash
./03-runner/run_benchmark.sh 5
```

## 进阶／CI

```bash
python3 03-runner/run_task.py --preflight
python3 03-runner/run_task.py --harness codex --model MODEL_ID --rolls 3
```

## 任务定义

题干、输出契约和判定标准见 `01-task/task.yaml`，它是本任务的唯一事实来源。
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 VeriForge benchmark 任务包骨架")
    parser.add_argument("slug", help="任务 slug，小写字母数字和连字符，例如 invoice-review")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="任务包的父目录（默认为当前目录）",
    )
    parser.add_argument("--title", help="任务标题（默认由 slug 推导）")
    parser.add_argument(
        "--version",
        default="v1",
        help="task_id 的版本后缀（默认 v1）",
    )
    parser.add_argument("--force", action="store_true", help="目标目录已存在时覆盖")
    return parser.parse_args()


def force_remove(target: Path) -> None:
    """删除任务包。runner 会把 roll workspace 中的 fixture 设为只读，
    直接 rmtree 会因权限失败，所以先恢复写权限。"""

    def on_error(func, path, _exc_info):
        parent = Path(path).parent
        parent.chmod(parent.stat().st_mode | stat.S_IRWXU)
        Path(path).chmod(stat.S_IRUSR | stat.S_IWUSR)
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=on_error)
    else:
        shutil.rmtree(target, onerror=lambda f, p, e: on_error(f, p, e))


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )


def replace_task_id(package: Path, task_id: str) -> list[str]:
    """把所有占位 task_id 替换成真实值，返回改动过的相对路径。"""
    touched = []
    for path in sorted(package.rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml", ".json", ".md", ".py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if PLACEHOLDER_TASK_ID not in text:
            continue
        path.write_text(text.replace(PLACEHOLDER_TASK_ID, task_id), encoding="utf-8")
        touched.append(str(path.relative_to(package)))
    return touched


def make_executable(package: Path) -> None:
    for relative in EXECUTABLE_PATHS:
        path = package / relative
        if path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def verify(package: Path) -> list[str]:
    """检查必需文件齐全、脚本可执行、无残留占位符。"""
    problems = []
    for relative in REQUIRED_PATHS:
        if not (package / relative).exists():
            problems.append(f"缺少必需文件：{relative}")
    for relative in EXECUTABLE_PATHS:
        path = package / relative
        if path.is_file() and not path.stat().st_mode & stat.S_IXUSR:
            problems.append(f"文件缺少可执行权限：{relative}")
    for path in package.rglob("*"):
        if path.is_file() and path.suffix in {".yaml", ".yml", ".json", ".md"}:
            if PLACEHOLDER_TASK_ID in path.read_text(encoding="utf-8"):
                problems.append(f"仍含占位 task_id：{path.relative_to(package)}")
    return problems


def main() -> int:
    args = parse_args()

    if not SLUG_PATTERN.match(args.slug):
        print(f"错误：slug 只能包含小写字母、数字和连字符：{args.slug!r}", file=sys.stderr)
        return 2

    package = (args.output_dir / args.slug).resolve()
    if package.exists():
        if not args.force:
            print(f"错误：目标目录已存在：{package}（用 --force 覆盖）", file=sys.stderr)
            return 2
        force_remove(package)

    task_id = f"{args.slug}-{args.version}"
    title = args.title or args.slug.replace("-", " ").title()

    task_template = REPO_ROOT / "templates" / "task"
    if not task_template.is_dir():
        print(f"错误：未找到骨架模板：{task_template}", file=sys.stderr)
        return 2

    copy_tree(task_template, package)

    for source_relative, target_relative in RUNNER_ASSETS:
        source = REPO_ROOT / source_relative
        if not source.is_file():
            print(f"错误：未找到模板文件：{source_relative}", file=sys.stderr)
            return 2
        target = package / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    # models.yaml 用的是活动矩阵自己的 task_id，单独替换
    models_path = package / "03-runner" / "models.yaml"
    models_text = models_path.read_text(encoding="utf-8")
    models_path.write_text(
        re.sub(r'^task_id:.*$', f'task_id: "{task_id}"', models_text, count=1, flags=re.MULTILINE),
        encoding="utf-8",
    )

    (package / "01-task" / "README.md").write_text(
        README_TEMPLATE.format(title=title, objective="用一句话说明 Agent 必须达成什么。"),
        encoding="utf-8",
    )
    (package / "results").mkdir(exist_ok=True)
    (package / "evidence").mkdir(exist_ok=True)

    replace_task_id(package, task_id)
    make_executable(package)

    problems = verify(package)
    if problems:
        print("生成完成但存在问题：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"已生成任务包：{package}")
    print(f"task_id：{task_id}")
    print()
    print("接下来需要人工完成：")
    print("  1. 01-task/task.yaml —— 填写题干、输出契约、编码表和约束")
    print("  2. 02-evaluation/fixtures/ —— 替换为真实的去标识化输入数据")
    print("  3. 02-evaluation/reference_answer/ —— 写出满分标答")
    print("  4. 02-evaluation/validators/ —— 保留函数签名，替换判定逻辑")
    print("  5. 02-evaluation/rubric.yaml 与 scorer.py —— 对齐维度、权重和通过线")
    print("  6. 03-runner/cc_agent.sh 与 codex_agent.sh —— 在 prompt 中复述输出契约")
    print("  7. 03-runner/dependency-manifest.yaml —— 填写只读检查得到的真实状态")
    print()
    print("完成后按 references/task-contract.md 的一致性锚点逐项核对，")
    print("并运行标答／畸形输出／fatal 违规／输出缺失四个 scorer 用例。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
