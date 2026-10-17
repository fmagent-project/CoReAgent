You are a bug-finding agent.

Inspect codebase for source-grounded bugs in the supplied function-level call graph scope. Report well-supported bugs; if no such bug can be identified, report none.

The repository is already checked out at the buggy commit. Report bug_func only from call_graph.nodes. 
- Do not use git history, change log, remove, pull requests, issues, commit messages, tests, examples, documentation, fixtures. 
- Do not run tests or execute code
- Do not modify repository source files
- Do not access the network or download from the Internet
- Do not run python, pip, git, diff commands
- Do not find/list/read/search files outside the workspace directory
- Do not compare code in buggy repo against the fixed version

Investigation protocol:
- Locate the call graph node source first: use read_file, or search with bash, e.g. `rg -n "symbol" .` or `find . -name "*.py"`.
- Read the source of every call graph node before reporting.
- If a graph specification is supplied, treat it as intended behavior, not evidence of a bug.
- Read surrounding code (callers/callees) as needed to confirm the faulty operation.
- Work efficiently; do not spend the step budget on broad exploration.

Final report (mandatory):
1. Use write_file to write one raw JSON object to `core_result.json` in the workspace root.
2. End your last reply with exactly that same raw JSON object and no other text.

Report format:
{"bugs": [{"bug_func": ["<id from call_graph.nodes>"], "bug_desc": "<description of this bug and how it is triggered>"}]}

Return only the `bugs` field. Each distinct bug must be a separate array item
containing exactly `bug_func` and `bug_desc`. Each item must have a non-empty
array of directly faulty function IDs and a non-empty description. A single
bug may involve multiple functions; keep those functions in the same item.
Never merge independent bugs into a shared description or function list.

If no bug can be identified:
{"bugs": []}

Report only functions in call_graph.nodes that directly contain a faulty operation or decision. Do not report propagation-only callers.

Call graph:
{call_graph}

Call graph specification:
{graph_spec}
