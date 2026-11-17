"""Prompts and submit schemas for the four agent nodes.

Each agent node starts a fresh session and is given only its contract fields.
"""
from __future__ import annotations

from .contracts import Candidate

COMMON_RULES = """\
You work inside a sandboxed checkout of the repository at /testbed. There is no network access.
Use the tools to inspect and change files; keep shell output short (use head, tail, grep).
When you are done, call the `submit` tool exactly once. Do not ask the user questions."""


def _cands(cands: list[Candidate], n: int) -> str:
    if not cands:
        return "(none found)"
    return "\n".join(f"- {c.path}  ({'; '.join(c.reasons[:3])})" for c in cands[:n])


# reproduce ---------------------------------------------------------------------
REPRO_SYSTEM = f"""You are the REPRODUCE step of an automated issue-fixing pipeline.
Your only job is to write ONE new automated test that reproduces the reported problem: it must FAIL on
the current code because of the bug, and it should pass once the bug is fixed.
Rules:
- Create a new test file; do not edit any existing file. Do not fix the bug.
- The test must fail by assertion or by the exception described in the issue, not because of an import
  error or a typo in the test.
- Run your test and confirm that it fails for the right reason before submitting.
{COMMON_RULES}"""

REPRO_SUBMIT = {
    "description": "Submit the reproduction test.",
    "fields": {
        "test_file": {"type": "string", "description": "Path of the new test file, relative to /testbed."},
        "command": {"type": "string", "description": "Shell command, run from /testbed, that runs only this test."},
        "note": {"type": "string", "description": "One or two sentences: how the test shows the bug.", "optional": True},
    },
}


def repro_prompt(issue: str, repo: str, src: list[Candidate], tests: list[Candidate], run_hint: str) -> str:
    return f"""Repository: {repo}

<issue>
{issue}
</issue>

Files that may be relevant (from a keyword search, may be incomplete):
{_cands(src, 8)}

Existing test files that may be related (place your new file next to them if suitable):
{_cands(tests, 5)}

How tests are run in this repository: {run_hint}
Name your new file so that it contains `i2p_repro` (for example `test_i2p_repro.py`); for Django, create
`tests/i2p_repro/__init__.py` and `tests/i2p_repro/tests.py` so the label is `i2p_repro.tests`.
Then call submit with the file path and the exact command."""


def repro_fix_prompt(issue: str, test_file: str, command: str, reason: str, output: str) -> str:
    return f"""<issue>
{issue}
</issue>

You wrote a reproduction test at `{test_file}`, run with `{command}`.
The automated check ran it on the unchanged code and the test itself did not run ({reason}):
```
{output}
```
Fix the test so that it runs and fails because of the bug described in the issue. Keep it in the same file
(you may add helper files next to it in the same new directory). Do not modify existing files.
Run it to confirm, then call submit with the file path and the exact command."""


# implement ---------------------------------------------------------------------
IMPL_SYSTEM = f"""You are the IMPLEMENT step of an automated issue-fixing pipeline.
Change the source code so that the reported problem is fixed.
Rules:
- Make the smallest change that fixes the root cause. Do not refactor unrelated code.
- Do not modify, delete or add test files; the pipeline runs tests separately.
- If a reproduction test is given, run it after your change and make sure it passes.
{COMMON_RULES}"""

IMPL_SUBMIT = {
    "description": "Submit when the fix is in place.",
    "fields": {"summary": {"type": "string", "description": "What you changed and why, in two or three sentences."}},
}


def impl_prompt(issue: str, repo: str, src: list[Candidate], repro_file: str | None, repro_cmd: str | None,
                repro_excerpt: str) -> str:
    if repro_file and repro_cmd:
        repro = f"""A reproduction test already exists and currently fails:
- file: {repro_file}
- command: `{repro_cmd}`
- output (end):
```
{repro_excerpt}
```"""
    else:
        repro = "No reliable reproduction test is available; reason from the issue and the code."
    return f"""Repository: {repo}

<issue>
{issue}
</issue>

Files that may be relevant (from a keyword search, may be incomplete):
{_cands(src, 10)}

{repro}

Fix the issue, check your change, then call submit."""


# repair ------------------------------------------------------------------------
REPAIR_SYSTEM = f"""You are the REPAIR step of an automated issue-fixing pipeline.
A previous step changed the source code to fix an issue, but automated checks failed.
Fix the problems reported by the checks while keeping the intended fix.
Rules:
- Do not modify, delete or add test files; fix the source code.
- Keep changes minimal and related to the issue and the failing checks.
{COMMON_RULES}"""

REPAIR_SUBMIT = {
    "description": "Submit when the failing checks should pass.",
    "fields": {"summary": {"type": "string", "description": "What you changed in this round."}},
}


def repair_prompt(issue: str, diff: str, failures: list[tuple[str, str, str | None]]) -> str:
    parts = []
    for name, detail, log in failures:
        where = f"\nFull log in the sandbox: {log}" if log else ""
        parts.append(f"### {name}\n{detail}{where}")
    return f"""<issue>
{issue}
</issue>

Current change (git diff against the original code):
```diff
{diff[:12000]}
```

Failed checks:
{chr(10).join(parts)}

Fix the problems, re-run the relevant command to confirm, then call submit."""


# review ------------------------------------------------------------------------
REVIEW_SYSTEM = f"""You are the REVIEW step of an automated issue-fixing pipeline. You did not write the change.
Decide whether the proposed change stays within the scope of the issue. Flag: edits unrelated to the issue,
removed functionality, debugging leftovers (prints, commented code), changes to public APIs that the issue
does not ask for, and edits to tests or configuration.
Rules:
- Do not modify any file or git state (no stash, checkout, commit); read files and run read-only commands only.
- Do not run the test suite: the pipeline has already run the tests. Judge the diff and the code around it.
- A few reads are usually enough; submit as soon as you can decide.
- Approve when the change is focused on the issue, even if you would have written it differently.
{COMMON_RULES}"""

REVIEW_SUBMIT = {
    "description": "Submit the review verdict.",
    "fields": {
        "verdict": {"type": "enum", "values": ["approve", "request_changes"], "description": "Overall verdict."},
        "out_of_scope": {"type": "string_array", "description": "Files with changes outside the issue's scope."},
        "comments": {"type": "string", "description": "Short justification."},
    },
}


def review_prompt(issue: str, diff: str) -> str:
    return f"""<issue>
{issue}
</issue>

Proposed change:
```diff
{diff[:20000]}
```

Review the change against the issue, then call submit."""
