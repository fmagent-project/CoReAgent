#!/usr/bin/env python
"""CoReAgent: run Mini-Agent on a CoRe benchmark bug-finding task.

For each input file under <task_dir>/input/*_input.json this script:
  1. maintains a shared bare git store per repository under .repos/
     (cloned once, reused across cases), and checks out a dedicated git
     worktree per case at buggy_commit;
  2. builds a bug-finding prompt (template loaded from CoReAgent.md) from the
     input's call_graph and graph_spec;
  3. runs `uv run python -m mini_agent.cli --workspace <worktree> --task <prompt>`
     with the per-case worktree as the agent workspace;
  4. writes the normalized result to <task_dir>/CoReAgent-<model>/<case>_output.json
     (or <task_dir>/<output_dir>/<case>_output.json when --output-dir is given),
     the console run log to <case>_agent.log, and copies the mini_agent run log
     (~/.mini-agent/log/agent_run_*.log) into the same directory under its
     original filename.

Concurrency: multiple cases may run in parallel. Each case gets its own
worktree (so checkouts and the agent's file reads never interfere), and all
metadata operations on a shared store (clone/fetch/worktree add/remove) are
serialized with advisory file locks under .repos/.locks/.

Layout example:
    .repos/django_django.git            # bare blobless store (shared)
    .repos/django_django_26552/         # per-case worktree (<owner_repo>_<issuenumber>)
    .repos/.locks/django_django.lock    # store lock
    .repos/.locks/django_django_26552.lock       # per-case lock

Multiple-case mode: with --workers [N] (N defaults to 4), DIR is an
<owner_repo> directory (e.g. CoRe_bench_lite/django_django) or a <benchmark>
directory (e.g. CoRe_bench_lite) holding several <owner_repo> directories.
Every case found under DIR is run, up to N cases concurrently.

Usage:
    uv run python CoReAgent.py <task_dir> [--repos-dir DIR] [--output-dir DIR] [--force] [--model MODEL]
    uv run python CoReAgent.py <owner_repo_dir> --workers [N] [--model MODEL]
    uv run python CoReAgent.py <benchmark_dir> --workers [N] [--model MODEL]
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

# The result file the agent is instructed to write inside its workspace.
RESULT_FILE = "core_result.json"

NO_BUG_MESSAGE = (
    "No source-grounded bug candidate was reported within the provided call graph scope."
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def log(message: str) -> None:
    """Print a progress line to stderr so stdout stays clean for results."""
    print(message, file=sys.stderr, flush=True)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def run_cmd(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a command, returning the CompletedProcess (output captured as text)."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_agent_streaming(
    command: list[str],
    *,
    cwd: Path,
    case_log_path: Path,
    header: str,
    env: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Run mini_agent, streaming its stdout/stderr into case_log_path in real time.

    The header (task metadata + prompt) is written first, then every output
    line is appended and flushed as it arrives, so the case log can be tailed
    while the agent is still running. Returns ``(stdout_text, exit_code)``
    after the process exits.
    """
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # Unbuffered child output: without this, the child Python block-buffers
        # its stdout (it is a pipe, not a TTY) and no line reaches the
        # streaming loop until ~8KB accumulate or the process exits.
        env=env if env is not None else {**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    write_lock = threading.Lock()
    stdout_parts: list[str] = []
    stderr_started = False

    def consume(stream: Any, parts: list[str], is_stderr: bool) -> None:
        nonlocal stderr_started
        for line in iter(stream.readline, ""):
            if not line:
                break
            parts.append(line)
            with write_lock:
                if is_stderr and not stderr_started:
                    stderr_started = True
                    log_file.write("\n--- stderr ---\n")
                log_file.write(line)
                log_file.flush()
            if not is_stderr:
                match = re.search(r"Log file:\s*(\S+)", strip_ansi(line))
                if match:
                    log(f"  mini_agent log: {match.group(1)}")

    with open(case_log_path, "w", encoding="utf-8") as log_file:
        log_file.write(header)
        log_file.flush()

        stdout_thread = threading.Thread(
            target=consume, args=(proc.stdout, stdout_parts, False), daemon=True
        )
        stderr_thread = threading.Thread(
            target=consume, args=(proc.stderr, [], True), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            exit_code = proc.wait()
        except KeyboardInterrupt:
            # Ctrl+C is delivered to the foreground process group, but make
            # termination explicit so cleanup never races a live agent.
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait()
            raise
        finally:
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)

    return "".join(stdout_parts), exit_code


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on lock_path for the context duration.

    On platforms without fcntl the lock degrades to a no-op.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Environment check
# ---------------------------------------------------------------------------

def check_environment() -> None:
    """Fail early when required tools are missing.

    The bug-finding prompt tells the agent to search the repository with rg
    (ripgrep), so it must be on PATH before any case runs.
    """
    if shutil.which("rg") is None:
        raise RuntimeError(
            "rg (ripgrep) is not available on PATH; the bug-finding prompt relies "
            "on it for code search. Install it (e.g. `brew install ripgrep`) and retry."
        )


def output_dir_name(value: str) -> Path:
    """Parse --output-dir as a safe path relative to each case directory."""
    path = Path(value)
    parts = value.split("/")
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in value
    ):
        raise argparse.ArgumentTypeError(
            "--output-dir must be a relative path without '.' or '..' components, "
            "such as 'CoReAgent-model/run1'; absolute paths are not supported"
        )
    return path


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------

def find_input_files(task_dir: Path) -> list[Path]:
    """Find input JSON files in the task directory.

    Searches <task_dir>/input/*_input.json first, then any *.json under
    <task_dir>/input/, then <task_dir>/input.json.
    """
    input_dir = task_dir / "input"
    candidates: list[Path] = []
    if input_dir.is_dir():
        candidates = sorted(input_dir.glob("*_input.json"))
        if not candidates:
            candidates = sorted(p for p in input_dir.glob("*.json") if p.is_file())
    if not candidates and (task_dir / "input.json").is_file():
        candidates = [task_dir / "input.json"]
    return candidates


def case_name(input_path: Path) -> str:
    """Derive the case name from the input file name.

    django_django_26552_input.json -> django_django_26552
    """
    stem = input_path.stem
    if stem.endswith("_input"):
        stem = stem[: -len("_input")]
    return stem


def discover_case_dirs(base_dir: Path) -> list[Path]:
    """Find benchmark case directories under an <owner_repo> or <benchmark> directory.

    Returns the directory itself if it already contains input files;
    otherwise its immediate subdirectories that do; otherwise recurses one
    level deeper (a <benchmark> directory holds <owner_repo> directories,
    each of which holds the case directories).
    """
    if find_input_files(base_dir):
        return [base_dir]
    direct = sorted(
        subdir
        for subdir in base_dir.iterdir()
        if subdir.is_dir() and find_input_files(subdir)
    )
    if direct:
        return direct
    cases: list[Path] = []
    for subdir in sorted(d for d in base_dir.iterdir() if d.is_dir()):
        cases.extend(discover_case_dirs(subdir))
    return cases


# ---------------------------------------------------------------------------
# Repository setup (concurrency-safe: shared bare store + per-case worktrees)
# ---------------------------------------------------------------------------

def repo_dir_name(repo_url: str) -> str:
    """Derive the store directory name from the repository URL.

    https://github.com/django/django -> django_django
    """
    cleaned = repo_url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    parts = [p for p in cleaned.split("/") if p and not p.endswith(":")]
    if len(parts) >= 2:
        return "_".join(parts[-2:])
    return parts[-1] if parts else "repo"


def store_lock_path(repos_dir: Path, repo_name: str) -> Path:
    return repos_dir / ".locks" / f"{repo_name}.lock"


def ensure_store(repo_url: str, commit: str, repos_dir: Path) -> Path:
    """Clone (once) and make sure the commit is available in the bare store.

    All operations are serialized by the per-repository store lock, so any
    number of cases can share the store concurrently.
    """
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_name = repo_dir_name(repo_url)
    store_dir = repos_dir / f"{repo_name}.git"

    with file_lock(store_lock_path(repos_dir, repo_name)):
        if not store_dir.exists():
            log(f"Cloning {repo_url} -> {store_dir}")
            result = run_cmd(
                ["git", "clone", "--bare", "--filter=blob:none", repo_url, str(store_dir)],
                timeout=600,
            )
            if result.returncode != 0:
                # Fall back to a plain clone for hosts that reject partial clones.
                log("Blobless clone failed, retrying with a plain clone...")
                result = run_cmd(
                    ["git", "clone", "--bare", repo_url, str(store_dir)],
                    timeout=600,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

        available = run_cmd(
            ["git", "-C", str(store_dir), "cat-file", "-e", f"{commit}^{{commit}}"]
        )
        if available.returncode != 0:
            log(f"Fetching {commit} into {store_dir}")
            fetch = run_cmd(
                ["git", "-C", str(store_dir), "fetch", "--filter=blob:none", "origin", commit]
            )
            if fetch.returncode != 0:
                fetch = run_cmd(["git", "-C", str(store_dir), "fetch", "origin"])
                if fetch.returncode != 0:
                    raise RuntimeError(f"git fetch failed: {fetch.stderr.strip()}")
            available = run_cmd(
                ["git", "-C", str(store_dir), "cat-file", "-e", f"{commit}^{{commit}}"]
            )
            if available.returncode != 0:
                raise RuntimeError(
                    f"commit {commit} not found in {repo_url} after fetch"
                )
    return store_dir


def worktree_path(repos_dir: Path, repo_name: str, name: str) -> Path:
    """Per-case worktree path: .repos/<owner_repo>_<issuenumber>.

    The case name normally already carries the owner_repo prefix (e.g.
    django_django_26552), so the prefix is stripped to avoid repeating it.
    """
    if name.startswith(repo_name + "_"):
        name = name[len(repo_name) + 1:]
    return repos_dir / f"{repo_name}_{name}"


def ensure_worktree(store_dir: Path, commit: str, wt_path: Path) -> None:
    """Create (or recreate) the per-case worktree checked out at commit.

    Serialized by the store lock. Each case has its own worktree directory,
    so concurrent agents never see each other's checkouts.
    """
    with file_lock(store_lock_path(store_dir.parent, store_dir.name[: -len(".git")])):
        run_cmd(["git", "-C", str(store_dir), "worktree", "prune"])
        if wt_path.exists():
            removed = run_cmd(
                ["git", "-C", str(store_dir), "worktree", "remove", "--force", str(wt_path)]
            )
            if removed.returncode != 0:
                shutil.rmtree(wt_path, ignore_errors=True)
                run_cmd(["git", "-C", str(store_dir), "worktree", "prune"])
        log(f"Checking out {commit} in {wt_path}")
        result = run_cmd(
            ["git", "-C", str(store_dir), "worktree", "add", "--force", "--detach", str(wt_path), commit]
        )
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    head = run_cmd(["git", "-C", str(wt_path), "rev-parse", "HEAD"])
    checked_out = head.stdout.strip()
    if checked_out != commit:
        raise RuntimeError(f"checked out {checked_out}, expected {commit}")


def remove_worktree(store_dir: Path, wt_path: Path) -> None:
    """Remove the per-case worktree (serialized by the store lock)."""
    with file_lock(store_lock_path(store_dir.parent, store_dir.name[: -len(".git")])):
        if not wt_path.exists():
            return
        result = run_cmd(
            ["git", "-C", str(store_dir), "worktree", "remove", "--force", str(wt_path)]
        )
        if result.returncode != 0:
            shutil.rmtree(wt_path, ignore_errors=True)
            run_cmd(["git", "-C", str(store_dir), "worktree", "prune"])


# ---------------------------------------------------------------------------
# Model name (from the Mini-Agent config that will actually be used)
# ---------------------------------------------------------------------------

def get_model_name() -> str:
    """Read the configured model name from Mini-Agent's config.yaml."""
    try:
        from mini_agent.config import Config

        config_path = Config.get_default_config_path()
        config = Config.from_yaml(config_path)
        return config.llm.model
    except Exception as error:  # fall back to a naive YAML scan
        config_path = Path(__file__).parent / "mini_agent" / "config" / "config.yaml"
        if config_path.exists():
            for line in config_path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^\s*model\s*:\s*[\"']?([^\"'#\s]+)", line)
                if match:
                    return match.group(1)
        raise RuntimeError(f"could not determine model name: {error}") from error


def make_config_override_home(project_root: Path, model: str) -> tuple[Path, dict[str, str]]:
    """Create a temp HOME whose ~/.mini-agent/config/config.yaml overrides the model.

    mini_agent resolves its config by priority: <cwd>/mini_agent/config/, then
    ~/.mini-agent/config/. Running the subprocess with cwd and HOME pointing at
    the returned temp dir (which contains no mini_agent/ directory, so the real
    package is not shadowed on sys.path) makes it pick up the copied config with
    the requested model. Returns (override_home, subprocess_env).
    """
    override_home = Path(tempfile.mkdtemp(prefix="coreagent_home_"))
    config_dir = override_home / ".mini-agent" / "config"
    config_dir.mkdir(parents=True)
    try:
        from mini_agent.config import Config

        source_dir = Config.get_default_config_path().parent
    except Exception:
        source_dir = project_root / "mini_agent" / "config"
    for item in source_dir.iterdir():
        if item.is_file():
            shutil.copyfile(item, config_dir / item.name)
    config_file = config_dir / "config.yaml"
    text = config_file.read_text(encoding="utf-8")
    text = re.sub(
        r"(?m)^(\s*model\s*:\s*).*$",
        lambda match: match.group(1) + json.dumps(model),
        text,
        count=1,
    )
    config_file.write_text(text, encoding="utf-8")

    env = dict(os.environ)
    env["HOME"] = str(override_home)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(project_root)
        if not existing_pythonpath
        else str(project_root) + os.pathsep + existing_pythonpath
    )
    # Keep uv's package cache from the real home so --project runs do not
    # re-download packages into the temp home.
    original_home = os.environ.get("HOME")
    if original_home:
        cache = Path(original_home) / ".cache" / "uv"
        if cache.is_dir():
            env["UV_CACHE_DIR"] = str(cache)
    return override_home, env


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def load_prompt_template() -> str:
    """Load the bug-finding prompt template from CoReAgent.md (next to this script)."""
    prompt_file = Path(__file__).with_name("CoReAgent.md")
    if not prompt_file.is_file():
        raise RuntimeError(f"prompt template not found: {prompt_file}")
    return prompt_file.read_text(encoding="utf-8")


def build_prompt(input_data: dict[str, Any]) -> str:
    call_graph = input_data.get("call_graph")
    if not isinstance(call_graph, dict):
        raise RuntimeError("input must contain a call_graph object")
    prompt = load_prompt_template().replace(
        "{call_graph}", json.dumps(call_graph, indent=2, ensure_ascii=False)
    )
    graph_spec = input_data.get("graph_spec")
    graph_spec = graph_spec.strip() if isinstance(graph_spec, str) else ""
    prompt = prompt.replace("{graph_spec}", graph_spec)
    if not graph_spec:
        # Drop the graph-spec section entirely when the input has none.
        prompt = prompt.replace("Call graph specification:\n", "")
    return prompt


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------

def _saved_log_line(saved_log: Path | None) -> str:
    """One-line pointer in the case log to the saved mini_agent run log."""
    if saved_log is None:
        return "\n--- mini_agent log file: not reported ---\n"
    return f"\n--- mini_agent log copied to: {saved_log} ---\n"


def find_mini_agent_log(captured_stdout: str) -> str | None:
    """Locate the mini_agent run log path printed in the captured stdout.

    mini_agent prints "Log file: <path>" at the start of every agent run; the
    log itself lives under ~/.mini-agent/log/. Returns the path, or None when
    no log file was reported.
    """
    match = re.search(r"Log file:\s*(\S+)", strip_ansi(captured_stdout))
    if not match:
        return None
    return match.group(1).strip()


def save_mini_agent_log(log_path: str | None, model_dir: Path) -> Path | None:
    """Copy the mini_agent run log into model_dir, keeping its original filename.

    Returns the destination path, or None when there is nothing to copy.
    """
    if not log_path:
        return None
    source = Path(log_path)
    if not source.is_file():
        return None
    dest = model_dir / source.name
    counter = 1
    while dest.exists():
        # Same-name collision guard: agent_run_xxx.log -> agent_run_xxx-1.log
        dest = model_dir / f"{source.stem}-{counter}{source.suffix}"
        counter += 1
    shutil.copyfile(source, dest)
    return dest

def _find_json_objects(text: str) -> list[Any]:
    """Find every JSON value in the text, preferring objects that mention bug_func."""
    results: list[Any] = []
    for match in re.finditer(
        r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL
    ):
        try:
            results.append(json.loads(match.group(1).strip()))
        except json.JSONDecodeError:
            pass
    # Single-level objects containing bug_func (evidence arrays are fine here).
    for match in re.finditer(r"\{[^{}]*?\"bug_func\"[^{}]*?\}", text, flags=re.DOTALL):
        try:
            results.append(json.loads(match.group(0)))
        except json.JSONDecodeError:
            pass
    # Brace-matched decode from the nearest '{' before each "bug_func" mention,
    # which handles nested JSON that the flat regex misses.
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\"bug_func\"", text):
        start = text.rfind("{", max(0, match.start() - 2000), match.start())
        if start >= 0:
            try:
                results.append(decoder.raw_decode(text[start:])[0])
            except json.JSONDecodeError:
                pass
    return results


def parse_json_text(text: str) -> Any:
    """Parse a raw JSON value from text, tolerating Markdown fences."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    fenced = re.fullmatch(
        r"\s*```(?:json)?\s*(.*?)\s*```\s*", stripped, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        return json.loads(fenced.group(1))
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", stripped):
        try:
            value, end = decoder.raw_decode(stripped[match.start():])
        except json.JSONDecodeError:
            continue
        if not stripped[match.start() + end:].strip():
            return value
    raise ValueError("text does not contain a single JSON value")


def extract_result(wt_path: Path, captured_stdout: str) -> dict[str, Any]:
    """Extract the agent's final bug report.

    Preference order:
      1. <wt_path>/core_result.json written by the agent (instructed in prompt);
      2. the last JSON object mentioning bug_func in the captured stdout.
    """
    result_file = wt_path / RESULT_FILE
    if result_file.is_file():
        try:
            return parse_json_text(result_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            log(f"Warning: could not parse {result_file}: {error}")
    for candidate in reversed(_find_json_objects(strip_ansi(captured_stdout))):
        if isinstance(candidate, dict) and (
            "bug_func" in candidate or "result" in candidate
        ):
            return candidate
    raise RuntimeError(
        "no result found: agent did not write core_result.json or a final JSON report"
    )


def normalize_candidate(result: Any) -> dict[str, Any]:
    """Normalize the agent report to the benchmark candidate format."""
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        result = result["result"]
    if not isinstance(result, dict):
        raise RuntimeError("agent report must be a JSON object")
    raw_bug_func = result.get("bug_func") or []
    if isinstance(raw_bug_func, str):
        raw_bug_func = [raw_bug_func]
    bug_func = []
    for function_id in raw_bug_func:
        if isinstance(function_id, str) and function_id.strip():
            if function_id.strip() not in bug_func:
                bug_func.append(function_id.strip())
    bug_desc = result.get("bug_desc")
    bug_desc = bug_desc.strip() if isinstance(bug_desc, str) else ""
    if not bug_func:
        return {"bug_func": [], "bug_desc": NO_BUG_MESSAGE}
    return {
        "bug_func": bug_func,
        "bug_desc": bug_desc or "Bug reported without a description.",
    }


# ---------------------------------------------------------------------------
# Per-case pipeline
# ---------------------------------------------------------------------------

def _process_input(
    input_path: Path,
    *,
    task_dir: Path,
    repos_dir: Path,
    project_root: Path,
    model: str,
    output_dir: Path | None,
    config_override_home: Path | None,
    subprocess_env: dict[str, str] | None,
    force: bool,
) -> int:
    """Run the bug-finding pipeline for one input file. Returns the process exit code."""
    name = case_name(input_path)
    try:
        input_data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        log(f"error: invalid JSON in {input_path}: {error}")
        return 1
    repo_url = input_data.get("repo")
    buggy_commit = input_data.get("buggy_commit")
    if not isinstance(repo_url, str) or not repo_url.strip():
        log(f"error: {input_path}: missing repo URL")
        return 1
    if not isinstance(buggy_commit, str) or not buggy_commit.strip():
        log(f"error: {input_path}: missing buggy_commit")
        return 1

    # The per-case lock spans the whole run so the same case launched twice
    # concurrently does not double-execute or write the same outputs.
    with file_lock(repos_dir / ".locks" / f"{name}.lock"):
        if output_dir is None:
            model_dir = task_dir / f"CoReAgent-{model}"
        else:
            # task_dir is the individual case directory in both modes.
            model_dir = task_dir / output_dir
        output_path = model_dir / f"{name}_output.json"
        case_log_path = model_dir / f"{name}_agent.log"
        if output_path.exists() and not force:
            log(f"Skipping {name}: {output_path} already exists (use --force to rerun)")
            return 0
        model_dir.mkdir(parents=True, exist_ok=True)

        log(f"=== {name} ===")
        log(f"  input:  {input_path}")
        log(f"  repo:   {repo_url}")
        log(f"  commit: {buggy_commit}")

        try:
            store_dir = ensure_store(repo_url, buggy_commit, repos_dir)
            wt_path = worktree_path(repos_dir, repo_dir_name(repo_url), name)
            ensure_worktree(store_dir, buggy_commit, wt_path)
            prompt = build_prompt(input_data)
        except RuntimeError as error:
            log(f"error: {name}: {error}")
            return 1

        # The agent is told to write its report here; remove any stale copy first.
        (wt_path / RESULT_FILE).unlink(missing_ok=True)

        # Reuse the interpreter running CoReAgent.  When invoked from the
        # benchmark wrapper this is benchmark's virtualenv, so Mini-Agent
        # does not silently switch to a second submodule environment.
        command = [
            sys.executable,
            "-u",  # unbuffered stdout/stderr: stream console output line by line
            "-m",
            "mini_agent.cli",
            "--workspace",
            str(wt_path),
            "--task",
            prompt,
        ]
        subprocess_cwd = (
            config_override_home if config_override_home is not None else project_root
        )

        command_prefix = " ".join(command[:-2])  # without --task and the prompt
        header = [
            f"CoReAgent run for {name}",
            f"input: {input_path}",
            f"repo: {repo_url}",
            f"buggy_commit: {buggy_commit}",
            f"workspace: {wt_path}",
            f"model: {model}",
            f"command: {command_prefix} --task <prompt below>",
            "",
            "--- task prompt ---",
            prompt,
            "--- mini_agent output ---",
        ]
        captured_stdout = ""

        log(f"  running: {command_prefix} --task <prompt>")
        captured_stdout, exit_code = run_agent_streaming(
            command,
            cwd=subprocess_cwd,
            case_log_path=case_log_path,
            header="\n".join(header) + "\n",
            env=subprocess_env,
        )

        log(f"  mini_agent exited with code {exit_code}")
        # The agent_run log is copied only after the run completes.
        saved_log = save_mini_agent_log(find_mini_agent_log(captured_stdout), model_dir)

        try:
            result = extract_result(wt_path, captured_stdout)
            candidate = normalize_candidate(result)
            result_text = json.dumps(candidate, indent=2, ensure_ascii=False)
        except (RuntimeError, ValueError) as error:
            result_text = json.dumps(
                {"bug_func": [], "bug_desc": NO_BUG_MESSAGE}, indent=2, ensure_ascii=False
            )
            log(f"  warning: {name}: {error}; wrote empty candidate")

        output_path.write_text(result_text + "\n", encoding="utf-8")
        with open(case_log_path, "a", encoding="utf-8") as log_file:
            log_file.write(_saved_log_line(saved_log))
            log_file.write(
                f"\n--- mini_agent exited with code {exit_code} ---\n"
                "\n--- result ---\n"
                f"{result_text}\n"
            )

        log(f"  result: {output_path}")
        log(f"  log:    {case_log_path}")

        return exit_code if exit_code is not None else 1


def _cleanup_case_resources(input_path: Path, repos_dir: Path) -> None:
    """Remove a case worktree after success, failure, or interruption."""
    try:
        input_data = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    repo_url = input_data.get("repo")
    if not isinstance(repo_url, str) or not repo_url.strip():
        return

    repo_name = repo_dir_name(repo_url)
    store_dir = repos_dir / f"{repo_name}.git"
    wt_path = worktree_path(repos_dir, repo_name, case_name(input_path))
    if store_dir.is_dir() and wt_path.exists():
        try:
            remove_worktree(store_dir, wt_path)
        except Exception as error:
            log(f"warning: could not remove worktree {wt_path}: {error}")


def process_input(
    input_path: Path,
    *,
    task_dir: Path,
    repos_dir: Path,
    project_root: Path,
    model: str,
    output_dir: Path | None,
    config_override_home: Path | None,
    subprocess_env: dict[str, str] | None,
    force: bool,
) -> int:
    """Run one case and always remove its worktree when the call returns."""
    try:
        return _process_input(
            input_path,
            task_dir=task_dir,
            repos_dir=repos_dir,
            project_root=project_root,
            model=model,
            output_dir=output_dir,
            config_override_home=config_override_home,
            subprocess_env=subprocess_env,
            force=force,
        )
    finally:
        _cleanup_case_resources(input_path, repos_dir)


def _cleanup_lock_files(repos_dir: Path) -> None:
    """Remove lock placeholders after all case workers have stopped."""
    locks_dir = repos_dir / ".locks"
    if not locks_dir.is_dir():
        return
    for lock_path in locks_dir.glob("*.lock"):
        try:
            lock_path.unlink()
        except OSError as error:
            log(f"warning: could not remove lock file {lock_path}: {error}")
    try:
        locks_dir.rmdir()
    except OSError:
        # It may contain a lock created by another evaluation process.
        pass


def _cleanup_all_case_resources(repos_dir: Path) -> None:
    """Best-effort last-resort cleanup, preserving only shared ``*.git`` stores."""
    if not repos_dir.is_dir():
        return
    for child in repos_dir.iterdir():
        if child.name == ".locks" or child.name.endswith(".git"):
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
    _cleanup_lock_files(repos_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task_dir", type=Path, help="benchmark task directory")
    parser.add_argument(
        "--repos-dir",
        type=Path,
        default=Path.cwd() / ".repos",
        help="directory for repository stores and worktrees (default: ./.repos)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override the model configured in Mini-Agent's config.yaml "
        "(default: use the model from config.yaml); also names the output "
        "directory CoReAgent-<model>",
    )
    parser.add_argument(
        "--output-dir",
        type=output_dir_name,
        default=None,
        help="directory for result and log files (default: "
        "<case_dir>/CoReAgent-<model>); a relative path created under each "
        "case directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rerun cases whose output file already exists",
    )
    parser.add_argument(
        "--workers",
        type=int,
        nargs="?",
        const=4,
        default=None,
        help="run in multiple-case mode: DIR is an <owner_repo> or <benchmark> "
        "directory and every case found under it is run with up to N concurrent "
        "workers (default 4 when the value is omitted)",
    )
    args = parser.parse_args(argv)

    task_dir = args.task_dir.expanduser().resolve()
    if not task_dir.is_dir():
        log(f"error: task directory not found: {task_dir}")
        return 1
    repos_dir = args.repos_dir.expanduser().resolve()
    # Covers uncaught exceptions and KeyboardInterrupts that happen outside a
    # case worker, while the normal per-case cleanup remains more precise.
    atexit.register(_cleanup_all_case_resources, repos_dir)
    output_dir = args.output_dir
    project_root = Path(__file__).resolve().parent
    try:
        check_environment()
    except RuntimeError as error:
        log(f"error: {error}")
        return 1
    model = args.model or get_model_name()
    if args.model:
        log(f"Model: {model} (overriding config.yaml)")
    else:
        log(f"Model: {model}")
    config_override_home: Path | None = None
    subprocess_env: dict[str, str] | None = None
    if args.model:
        config_override_home, subprocess_env = make_config_override_home(
            project_root, model
        )
    try:
        if args.workers is None:
            # Single-case mode: DIR itself is one benchmark case directory.
            inputs = find_input_files(task_dir)
            if not inputs:
                log(f"error: no input JSON found under {task_dir}/input/")
                return 1
            log(f"Found {len(inputs)} input file(s): " + ", ".join(p.name for p in inputs))
            for input_path in inputs:
                code = process_input(
                    input_path,
                    task_dir=task_dir,
                    repos_dir=repos_dir,
                    project_root=project_root,
                    model=model,
                    output_dir=output_dir,
                    config_override_home=config_override_home,
                    subprocess_env=subprocess_env,
                    force=args.force,
                )
                if code != 0:
                    log(f"case {case_name(input_path)} finished with exit code {code}")
            log("Done.")
            return 0

        # Multiple-case mode: DIR is an <owner_repo> directory; every case
        # subdirectory runs with up to --workers concurrent workers.
        if args.workers < 1:
            log("error: --workers must be at least 1")
            return 1
        case_dirs = discover_case_dirs(task_dir)
        if not case_dirs:
            log(f"error: no case subdirectories with input JSON found under {task_dir}")
            return 1
        work_items = [
            (case_dir, input_path)
            for case_dir in case_dirs
            for input_path in find_input_files(case_dir)
        ]
        log(
            f"Found {len(case_dirs)} case dir(s), {len(work_items)} input file(s), "
            f"running with {args.workers} concurrent worker(s)"
        )

        failures = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    process_input,
                    input_path,
                    task_dir=case_dir,
                    repos_dir=repos_dir,
                    project_root=project_root,
                    model=model,
                    output_dir=output_dir,
                    config_override_home=config_override_home,
                    subprocess_env=subprocess_env,
                    force=args.force,
                ): (case_dir, input_path)
                for case_dir, input_path in work_items
            }
            for future in as_completed(futures):
                case_dir, input_path = futures[future]
                try:
                    code = future.result()
                except Exception:
                    log(f"case {case_name(input_path)} crashed:")
                    traceback.print_exc()
                    code = 1
                if code != 0:
                    failures += 1
                    log(f"case {case_name(input_path)} finished with exit code {code}")
        log("Done.")
        return 1 if failures else 0
    finally:
        _cleanup_lock_files(repos_dir)
        if config_override_home is not None:
            shutil.rmtree(config_override_home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
