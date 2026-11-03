#!/usr/bin/env python
"""Generate a focused fail-to-pass regression test for a bug candidate."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import CoReAgent as core


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"{label} file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid JSON in {label} file {path}: {error}") from error


def build_prompt(
    template: str,
    *,
    bug_report: Any,
    buggy_commit: str,
) -> str:
    values = {
        "{bug_report}": json.dumps(bug_report, indent=2, ensure_ascii=False),
        "{buggy_commit}": buggy_commit,
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def collect_patch(worktree: Path) -> str:
    staged = _git(worktree, "add", "-N", "--", ".")
    if staged.returncode:
        raise RuntimeError(f"could not prepare generated test diff: {staged.stderr.strip()}")
    changed = _git(worktree, "diff", "--name-only", "HEAD")
    if changed.returncode:
        raise RuntimeError(f"could not list generated changes: {changed.stderr.strip()}")
    paths = [line for line in changed.stdout.splitlines() if line]
    if not paths:
        raise RuntimeError("the model did not generate any test files")
    non_tests = [path for path in paths if not _is_test_path(path)]
    if non_tests:
        raise RuntimeError(
            "generated changes include non-test files: " + ", ".join(non_tests)
        )
    patch = _git(worktree, "diff", "--binary", "HEAD", "--", *paths)
    if patch.returncode:
        raise RuntimeError(f"could not export generated tests: {patch.stderr.strip()}")
    return patch.stdout


def validate_generation_result(data: Any) -> dict[str, Any]:
    required = {
        "tests",
        "buggy_command",
        "buggy_result",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise RuntimeError("f2p_result.json does not match the required report format")
    if (not isinstance(data["tests"], list) or not data["tests"]
            or any(not isinstance(path, str) or not path.strip() for path in data["tests"])):
        raise RuntimeError("f2p_result.json must list the generated test files")
    if not isinstance(data["buggy_command"], str) or not data["buggy_command"].strip():
        raise RuntimeError("f2p_result.json must include the buggy test command")
    if data["buggy_result"] != "fails":
        raise RuntimeError("the generated test did not verify a failure on buggy_commit")
    return data


def _is_test_path(value: str) -> bool:
    path = Path(value)
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return (
        "test" in parts
        or "tests" in parts
        or "testing" in parts
        or name.startswith("test_")
        or name.startswith("test")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or name.endswith("_test.rs")
        or name.endswith("_test.java")
        or name.endswith("_test.cs")
        or name.endswith("_test.cpp")
        or name.endswith("_test.cc")
        or name.endswith("_test.c")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
        or name.endswith("test.java")
        or name.endswith("test.cs")
        or name.endswith("_test.rb")
        or name.endswith("_test.php")
    )


def run_case(case_dir: Path, args: argparse.Namespace) -> int:
    inputs = core.find_input_files(case_dir)
    if len(inputs) != 1:
        raise RuntimeError(f"expected exactly one input JSON under {case_dir}, found {len(inputs)}")
    input_path = inputs[0]
    name = core.case_name(input_path)
    input_data = load_json(input_path, "input")
    candidate_dir = (
        case_dir / args.output_dir
        if args.output_dir
        else case_dir / f"CoReAgent-{args.model}"
    )
    candidate_path = candidate_dir / f"{name}_output.json"
    output_data = load_json(candidate_path, "candidate output")
    buggy_commit = input_data.get("buggy_commit")
    repo_url = input_data.get("repo")
    if not isinstance(buggy_commit, str) or not buggy_commit.strip():
        raise RuntimeError("input buggy_commit is missing")
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise RuntimeError("input repo URL is missing")
    result_path = candidate_dir / f"{name}_f2p.patch"
    if result_path.exists() and not args.force:
        print(f"Skipping: {result_path} already exists (use --force to rerun)", file=sys.stderr)
        return 0

    repos_dir = args.repos_dir.expanduser().resolve()
    model = args.model
    worktree_model = f"{model}-f2p"
    worktree = core.worktree_path(repos_dir, core.repo_dir_name(repo_url), name, worktree_model)
    store = core.ensure_store(repo_url, buggy_commit, repos_dir)
    prompt_file = Path(__file__).with_name("CoReGen.md")
    prompt = build_prompt(
        prompt_file.read_text(encoding="utf-8"),
        bug_report=output_data,
        buggy_commit=buggy_commit,
    )
    model_dir = candidate_dir
    model_dir.mkdir(parents=True, exist_ok=True)
    log_path = model_dir / f"{name}_gen.log"
    override_home = None
    subprocess_env = None
    try:
        if args.model_override:
            override_home, subprocess_env = core.make_config_override_home(Path(__file__).resolve().parent, model)
        core.ensure_worktree(store, buggy_commit, worktree)
        command = [
            sys.executable, "-u", "-m", "mini_agent.cli", "--workspace", str(worktree), "--task", prompt,
        ]
        cwd = override_home if override_home is not None else Path(__file__).resolve().parent
        header = (
            f"CoReGen run for {name}\ninput: {input_path}\n"
            f"candidate: {candidate_path}\nworkspace: {worktree}\n"
            f"buggy_commit: {buggy_commit}\n\n"
            f"--- task prompt ---\n{prompt}\n--- mini_agent output ---\n"
        )
        stdout, exit_code = core.run_agent_streaming(
            command, cwd=cwd, case_log_path=log_path, header=header, env=subprocess_env,
        )
        head = _git(worktree, "rev-parse", "HEAD")
        if head.returncode or head.stdout.strip().lower() != buggy_commit.lower():
            raise RuntimeError(
                "the generator must return the worktree to buggy_commit before finishing"
            )
        generated_result = validate_generation_result(
            load_json(worktree / "f2p_result.json", "F2P generation result")
        )
        (worktree / "f2p_result.json").unlink()
        patch_text = collect_patch(worktree)
        result_path.write_text(patch_text, encoding="utf-8")
        (candidate_dir / f"{name}_f2p.json").write_text(
            json.dumps(generated_result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Generated F2P patch: {result_path}")
        return exit_code if exit_code is not None else 1
    finally:
        if worktree.exists():
            core.remove_worktree(store, worktree)
        if override_home is not None:
            shutil.rmtree(override_home, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path, help="one benchmark case directory")
    parser.add_argument("--repos-dir", type=Path, default=Path.cwd() / ".repos")
    parser.add_argument("--model", default=None, help="select the matching CoReAgent output directory")
    parser.add_argument("--output-dir", type=core.output_dir_name)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    args.model_override = args.model is not None
    args.model = args.model or core.get_model_name()
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        task_dir = args.task_dir.expanduser().resolve()
        if not task_dir.is_dir():
            raise RuntimeError(f"task directory not found: {task_dir}")
        core.check_environment()
        return run_case(task_dir, args)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
