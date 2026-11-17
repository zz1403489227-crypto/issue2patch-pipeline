"""Fast unit tests for deterministic pieces (run by CI before the canary)."""
import random

from issue2patch.funnel import bootstrap_ci
from issue2patch.nodes.prefetch import extract
from issue2patch.repro_check import broken_run
from issue2patch.tasks import stratified_sample
from issue2patch.testing import is_test_path, newly_failing, related_test_files


def test_extract_ignores_dunders_and_finds_constants():
    paths, dotted, symbols = extract("Set `SECURE_REFERRER_POLICY` in django/conf/global_settings.py; "
                                     "ManagementUtility.__init__ calls CommandParser(prog=...)")
    assert "django/conf/global_settings.py" in paths
    assert "SECURE_REFERRER_POLICY" in symbols and "ManagementUtility" in symbols
    assert not any(s.startswith("__") for s in symbols)


def test_broken_run_flags_only_tests_that_did_not_run():
    assert broken_run("ImportError: Failed to import test module: tests", 1, "django")
    assert broken_run("whatever", 5, "pytest") == "pytest collected no tests"
    # an exception raised by the code under test is a real reproduction, not a broken test
    assert broken_run("ERROR: test_x\nTypeError: boom\nFAILED (errors=1)", 1, "django") is None
    assert broken_run("E   AssertionError", 1, "pytest") is None


def test_newly_failing_compares_against_baseline():
    before = {"a": "PASSED", "b": "FAILED", "c": "PASSED"}
    after = {"a": "PASSED", "b": "FAILED", "c": "FAILED", "d": "FAILED"}
    assert newly_failing(before, after) == ["c"]


def test_related_tests_prefer_matching_test_modules():
    files = ["pkg/core/mod.py", "tests/test_mod.py", "tests/roots/fixture/mod.py", "tests/test_other.py"]
    assert related_test_files(files, ["pkg/core/mod.py"], [], exclude=set()) == ["tests/test_mod.py"]
    assert is_test_path("tests/roots/fixture/mod.py") and not is_test_path("pkg/core/mod.py")


def test_stratified_sample_is_proportional_and_deterministic():
    ids = [f"a{i}" for i in range(60)] + [f"b{i}" for i in range(40)]
    s1 = stratified_sample(ids, lambda i: i[0], 10, random.Random(0))
    s2 = stratified_sample(ids, lambda i: i[0], 10, random.Random(0))
    assert s1 == s2 and sum(x.startswith("a") for x in s1) == 6


def test_bootstrap_interval_contains_mean():
    lo, hi = bootstrap_ci([0.0, 1.0] * 50)
    assert lo < 0.5 < hi
