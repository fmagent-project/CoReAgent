#!/usr/bin/env python
"""Judge a CoReAgent result against its oracle with an OpenAI model.

Usage:
    uv run python CoReJudge.py <owner_repo>/<issuenumber>/<CoReAgent-model>

The API key, base URL, and judge model are loaded exclusively from
mini_agent/config/config.yaml next to this script.

The candidate and oracle paths are derived from the supplied output directory:

    <output-dir>/<owner_repo_issuenumber>_output.json
    <output-dir>/../oracle/<owner_repo_issuenumber>_oracle.json

The result is written to:

    <output-dir>/<owner_repo_issuenumber>_judge.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from openai import BadRequestError, OpenAI


CONFIG_PATH = Path(__file__).parent / "mini_agent" / "config" / "config.yaml"
CANDIDATE_PLACEHOLDER = "{{CANDIDATE_DESCRIPTION_JSON}}"
ORACLE_PLACEHOLDER = "{{JUDGE_ORACLE_JSON}}"
NO_BUG_MESSAGE = (
    "No source-grounded bug candidate was reported within the provided call graph scope."
)

JUDGE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "core_judge_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "match": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["match", "reason"],
            "additionalProperties": False,
        },
    },
}

JUDGE_RETRY_ATTEMPTS = 3
JUDGE_RETRY_DELAY_SECONDS = 3
_RETRYABLE_ERROR_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectionError",
    "ReadTimeout",
    "TimeoutError",
}


@dataclass(frozen=True)
class JudgePaths:
    output_dir: Path
    candidate: Path
    oracle: Path
    result: Path


def derive_paths(output_dir: Path) -> JudgePaths:
    """Derive all case paths from <owner_repo>/<issue>/<model-dir>."""
    output_dir = output_dir.expanduser().resolve()
    if not output_dir.is_dir():
        raise RuntimeError(f"output directory does not exist: {output_dir}")

    issue_dir = output_dir.parent
    owner_repo_dir = issue_dir.parent
    if not issue_dir.name or not owner_repo_dir.name:
        raise RuntimeError(
            "output directory must have the form "
            "<owner_repo>/<issuenumber>/<CoReAgent-model>"
        )

    case_name = f"{owner_repo_dir.name}_{issue_dir.name}"
    return JudgePaths(
        output_dir=output_dir,
        candidate=output_dir / f"{case_name}_output.json",
        oracle=issue_dir / "oracle" / f"{case_name}_oracle.json",
        result=output_dir / f"{case_name}_judge.json",
    )


def read_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise RuntimeError(f"{label} file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid JSON in {label} file {path}: {error}") from error
    except OSError as error:
        raise RuntimeError(f"could not read {label} file {path}: {error}") from error


def load_prompt_template() -> str:
    prompt_path = Path(__file__).with_name("CoReJudge.md")
    if not prompt_path.is_file():
        raise RuntimeError(f"prompt template not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def build_prompt(candidate: Any, oracle: Any, template: str | None = None) -> str:
    """Insert the candidate and oracle JSON into CoReJudge.md."""
    template = load_prompt_template() if template is None else template
    missing = [
        token
        for token in (CANDIDATE_PLACEHOLDER, ORACLE_PLACEHOLDER)
        if token not in template
    ]
    if missing:
        raise RuntimeError(
            "prompt template is missing placeholder(s): " + ", ".join(missing)
        )
    return template.replace(
        CANDIDATE_PLACEHOLDER,
        json.dumps(candidate, ensure_ascii=False, indent=2),
    ).replace(
        ORACLE_PLACEHOLDER,
        json.dumps(oracle, ensure_ascii=False, indent=2),
    )


def parse_judge_result(content: str) -> dict[str, Any]:
    try:
        result = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"judge returned invalid JSON: {error}") from error

    if not isinstance(result, dict) or set(result) != {"match", "reason"}:
        raise RuntimeError(
            "judge result must be an object containing exactly match and reason"
        )
    if not isinstance(result["match"], bool):
        raise RuntimeError("judge result field 'match' must be a boolean")
    if not isinstance(result["reason"], str) or not result["reason"].strip():
        raise RuntimeError("judge result field 'reason' must be a non-empty string")
    return {"match": result["match"], "reason": result["reason"].strip()}


def call_judge(client: Any, model: str, prompt: str, provider: str) -> dict[str, Any]:
    if provider.lower() == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        content = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
        )
        if not content.strip():
            raise RuntimeError("judge returned empty content")
        return parse_judge_result(content)

    request = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = client.chat.completions.create(
            **request,
            response_format=JUDGE_RESPONSE_FORMAT,
        )
    except BadRequestError as error:
        # Some OpenAI-compatible APIs support JSON mode but not the stricter
        # json_schema response format. The prompt already specifies the exact
        # object shape, and parse_judge_result validates it after the call.
        if "response_format type is unavailable" not in str(error):
            raise
        response = client.chat.completions.create(
            **request,
            response_format={"type": "json_object"},
        )
    if not response.choices:
        raise RuntimeError("judge returned no choices")
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        refusal = getattr(response.choices[0].message, "refusal", None)
        if refusal:
            raise RuntimeError(f"judge refused the request: {refusal}")
        raise RuntimeError("judge returned empty content")
    return parse_judge_result(content)


def is_retryable_judge_error(error: BaseException) -> bool:
    """Return whether a judge failure is likely caused by connectivity."""
    return (
        isinstance(error, (ConnectionError, TimeoutError, OSError))
        or error.__class__.__name__ in _RETRYABLE_ERROR_NAMES
    )


def call_judge_with_retry(
    client: Any,
    model: str,
    prompt: str,
    provider: str,
) -> dict[str, Any]:
    """Call the judge, retrying connection failures twice after three seconds."""
    for attempt in range(JUDGE_RETRY_ATTEMPTS):
        try:
            return call_judge(client, model, prompt, provider)
        except Exception as error:
            if not is_retryable_judge_error(error) or attempt == JUDGE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(JUDGE_RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


def is_no_bug_candidate(candidate: Any) -> bool:
    """Return whether the candidate explicitly reports no bug."""
    return (
        isinstance(candidate, dict)
        and candidate.get("bug_func") == []
        and candidate.get("bug_desc") == NO_BUG_MESSAGE
    )


def write_judge_result(path: Path, result: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run(output_dir: Path) -> Path:
    paths = derive_paths(output_dir)
    candidate = read_json(paths.candidate, "candidate")

    # A normalized no-bug report is unambiguously a semantic non-match. Handle
    # it before reading the oracle or loading API configuration so this path
    # never initializes or calls the judge model.
    if is_no_bug_candidate(candidate):
        write_judge_result(
            paths.result,
            {"match": False, "reason": NO_BUG_MESSAGE},
        )
        return paths.result

    oracle = read_json(paths.oracle, "oracle")
    prompt = build_prompt(candidate, oracle)
    config = load_llm_config()
    provider = config.provider.lower()
    if provider == "anthropic":
        client = anthropic.Anthropic(
            api_key=config.api_key,
            base_url=config.api_base,
            default_headers={"Authorization": f"Bearer {config.api_key}"},
        )
    else:
        client = OpenAI(api_key=config.api_key, base_url=config.api_base)
    result = call_judge_with_retry(client, config.model, prompt, provider)
    write_judge_result(paths.result, result)
    return paths.result


def run_task(
    task_dir: Path,
    model: str | None = None,
    output_dir: str | None = None,
) -> list[Path]:
    """Judge candidates below a task directory, optionally selecting a model."""
    task_dir = task_dir.expanduser().resolve()
    if output_dir:
        output_dirs = [task_dir / output_dir]
    elif model:
        output_dirs = [task_dir / f"CoReAgent-{model}"]
    else:
        output_dirs = sorted(
            path for path in task_dir.iterdir()
            if path.is_dir() and path.name.startswith("CoReAgent-")
        )
    results = []
    for output_dir in output_dirs:
        if any(output_dir.glob("*_output.json")):
            results.append(run(output_dir))
    if not results:
        raise RuntimeError(f"no candidate output found under {task_dir}")
    return results


def load_llm_config() -> Any:
    """Load LLM settings from this repository's fixed config.yaml path."""
    from mini_agent.config import Config

    try:
        config = Config.from_yaml(CONFIG_PATH).llm
    except Exception as error:
        raise RuntimeError(f"could not load LLM config {CONFIG_PATH}: {error}") from error
    if not config.model.strip():
        raise RuntimeError(f"judge model is empty in {CONFIG_PATH}")
    return config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Judge one CoReAgent output directory against its oracle."
    )
    parser.add_argument(
        "task_dir",
        type=Path,
        help="task directory or a specific CoReAgent output directory",
    )
    parser.add_argument("--model", help="judge only CoReAgent-<model> output")
    # Accept CoReAgent options when evaluation forwards its argument list;
    # they are intentionally ignored by the judge.
    parser.add_argument("--repos-dir", type=Path)
    parser.add_argument("--output-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", nargs="?")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if any(args.task_dir.glob("*_output.json")):
            result_path = run(args.task_dir)
            print(result_path)
            return 0
        result_paths = run_task(args.task_dir, args.model, args.output_dir)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    for result_path in result_paths:
        print(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
