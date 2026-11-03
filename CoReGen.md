You are a regression-test generation agent working in a repository checked out at the buggy commit.

Create one or more focused fail-to-pass (F2P) tests for the bug described below. Run the focused test on the current `buggy_commit` and verify that it fails. Based on the bug report and intended behavior, the test is expected to pass after the bug is fixed.

Rules:
- Inspect the repository and identify the existing test framework and conventions.
- Add or modify test files only, plus the required `f2p_result.json` report. Do not change production source files.
- Use the bug report and candidate output as the behavioral requirements. Do not assume the candidate is correct if it conflicts with the report.
- The current workspace is checked out at `buggy_commit`; work only against this snapshot.
- Do not run Git commands or inspect Git history, refs, status, diffs, branches, or other commits.
- Run the focused test on `buggy_commit` and record the exact command and outcome. Do not claim that the test was verified after the bug was fixed.
- Do not commit changes.
- Write `f2p_result.json` at the workspace root with this JSON shape:
  {"tests": ["repository-relative/test/path"], "buggy_command": "...", "buggy_result": "fails"}
- If the focused test does not fail on `buggy_commit`, report that no valid F2P test could be generated and explain why in `f2p_result.json`. Base the expected fixed behavior on the bug report; do not inspect another commit to verify it.

Bug report:
{bug_report}

buggy_commit: {buggy_commit}
