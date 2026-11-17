"""Is a failing reproduction test failing because of the bug, or because the test itself cannot run?

Closed-loop case 1, second iteration (decision log 2026-11-16). After the first fix the reproduce node
submitted a test 95% of the time, but 18 of the 43 tests that also failed with the reference patch
applied had never run at all: the test module could not be imported or collected. Those failures are
visible on the original code, without any hidden information, so the node can catch them itself.

Only signals that mean "the test did not run" count. An exception raised by the code under test is a
legitimate reproduction (Django reports it as ERROR), so it is not treated as broken.
"""
from __future__ import annotations

import re

_PATTERNS = [
    (r"ImportError while importing test module", "import error in the test module"),
    (r"ERROR collecting|errors? during collection|Interrupted: \d+ errors? during collection", "collection error"),
    (r"ImportError: Failed to import test module", "Django could not import the test module"),
    (r"ModuleNotFoundError: No module named '(?:i2p_repro|tests?\.|test_)[^']*'", "test module not found"),
    (r"(?m)^E?\s*fixture '[^']+' not found", "missing pytest fixture"),
    (r"(?m)^Ran 0 tests", "no tests ran"),
    (r"(?m)^=+ no tests ran", "no tests ran"),
    (r"(?m)^SyntaxError|File \"[^\"]*i2p_repro[^\"]*\", line \d+\n.*\n\s*\^+\nSyntaxError", "syntax error in the test"),
    (r"file or directory not found|ERROR: not found: ", "test path not found"),
]


def broken_run(output: str, exit_code: int, framework: str) -> str | None:
    """A short reason if the test run shows the test itself did not run, else None."""
    if framework == "pytest" and exit_code in (2, 3, 4, 5):
        return {2: "pytest was interrupted (usually a collection error)", 3: "pytest internal error",
                4: "pytest usage error", 5: "pytest collected no tests"}[exit_code]
    for pat, why in _PATTERNS:
        if re.search(pat, output):
            return why
    return None
