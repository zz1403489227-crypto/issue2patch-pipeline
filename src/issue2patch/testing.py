"""Running a repository's own tests inside the sandbox.

Per-repo test commands and log parsers come from the SWE-bench harness (that is
environment knowledge, not hidden information). Which tests to run is decided
here from the changed files, never from the hidden test list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
from types import SimpleNamespace

from .sandbox import DockerSandbox, ExecResult

TEST_PATH_RE = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


@dataclass
class RepoTestInfo:
    repo: str
    framework: str  # pytest | django | sympy
    test_cmd: str
    eval_commands: list[str]
    test_spec: Any  # built from the sanitized instance; used only for log parsing

    @classmethod
    def build(cls, sanitized_instance: dict[str, Any]) -> "RepoTestInfo":
        repo, version = sanitized_instance["repo"], str(sanitized_instance["version"])
        specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
        cmd = specs["test_cmd"]
        if isinstance(cmd, list):
            cmd = cmd[-1]
        framework = "django" if repo == "django/django" else "sympy" if repo == "sympy/sympy" else "pytest"
        # Not make_test_spec(): for some repos it downloads requirements files from GitHub with no
        # timeout (a django task hung there). The Python log parsers never read the spec's fields.
        spec = SimpleNamespace(instance_id=sanitized_instance["instance_id"], repo=repo, version=version,
                               FAIL_TO_PASS=[], PASS_TO_PASS=[])
        return cls(repo, framework, cmd, list(specs.get("eval_commands", [])), spec)

    def directive(self, path: str) -> str:
        if self.framework == "django":
            p = path[:-3] if path.endswith(".py") else path
            p = p[len("tests/"):] if p.startswith("tests/") else p
            return p.replace("/", ".")
        return path

    def command_for(self, paths: list[str]) -> str:
        return f"{self.test_cmd} {' '.join(self.directive(p) for p in paths)}"

    def how_to_run_hint(self) -> str:
        if self.framework == "django":
            return ("Django's own runner, from /testbed: `./tests/runtests.py --verbosity 2 --settings=test_sqlite "
                    "--parallel 1 <module.label>` where a test file tests/<app>/<name>.py has label <app>.<name>.")
        if self.framework == "sympy":
            return "SymPy's runner, from /testbed: `bin/test -C --verbose <path/to/test_file.py>`."
        return f"pytest, from /testbed: `{self.test_cmd} <path/to/test_file.py>`."

    def run(self, sb: DockerSandbox, paths: list[str], timeout: int) -> tuple[ExecResult, dict[str, str]]:
        script = "\n".join([*self.eval_commands, self.command_for(paths)])
        res = sb.exec(script, timeout=timeout)
        try:
            statuses = MAP_REPO_TO_PARSER[self.repo](res.output, self.test_spec)
        except Exception:  # parser failure is reported as "no parsed tests"
            statuses = {}
        return res, statuses


def related_test_files(all_files: list[str], changed_sources: list[str], hinted: list[str],
                       exclude: set[str], limit: int = 3) -> list[str]:
    """Existing test files most likely to exercise the changed sources."""
    def is_test_module(f: str) -> bool:
        # a module the test runner collects, not a fixture that merely lives under tests/
        name = PurePosixPath(f).name
        return name.startswith("test_") or name.endswith("_test.py") or name == "tests.py" \
            or (name.startswith("test") and "/tests/" in f and PurePosixPath(f).parent.name == "tests")

    tests = [f for f in all_files if f.endswith(".py") and is_test_path(f) and f not in exclude
             and is_test_module(f)]
    picks: list[str] = []
    for src in changed_sources:
        stem = PurePosixPath(src).stem
        names = {f"test_{stem}.py", f"{stem}_test.py", f"test_{stem}s.py"}
        cands = [t for t in tests if PurePosixPath(t).name in names]
        src_parts = PurePosixPath(src).parts
        cands.sort(key=lambda t: -sum(1 for a, b in zip(PurePosixPath(t).parts, src_parts) if a == b))
        picks += cands[:1]
    picks += [h for h in hinted if h in tests]
    out: list[str] = []
    for p in picks:
        if p not in out:
            out.append(p)
    return out[:limit]


def newly_failing(before: dict[str, str], after: dict[str, str]) -> list[str]:
    ok = {"PASSED", "XFAIL"}
    return sorted(t for t, s in after.items() if s not in ok and before.get(t) in ok)
