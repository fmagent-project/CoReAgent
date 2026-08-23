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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI


CONFIG_PATH = Path(__file__).parent / "mini_agent" / "config" / "config.yaml"
CANDIDATE_PLACEHOLDER = "{{CANDIDATE_DESCRIPTION_JSON}}"
ORACLE_PLACEHOLDER = "{{JUDGE_ORACLE_JSON}}"

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


def call_judge(client: OpenAI, model: str, prompt: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format=JUDGE_RESPONSE_FORMAT,
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


def run(output_dir: Path, *, client: OpenAI, model: str) -> Path:
    paths = derive_paths(output_dir)
    candidate = read_json(paths.candidate, "candidate")
    oracle = read_json(paths.oracle, "oracle")
    prompt = build_prompt(candidate, oracle)
    result = call_judge(client, model, prompt)
    paths.result.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return paths.result


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
        "output_dir",
        type=Path,
        help="directory <owner_repo>/<issuenumber>/<CoReAgent-model>",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_llm_config()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        result_path = run(
            args.output_dir,
            client=OpenAI(api_key=config.api_key, base_url=config.api_base),
            model=config.model,
        )
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
