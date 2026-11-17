"""Run the official SWE-bench harness (swebench 4.1.0) unchanged, minus its GitHub downloads.

make_test_spec() builds every script up front, including the environment-image script, which
downloads requirements.txt / environment.yml from raw.githubusercontent.com with `requests` and no
timeout. Measured on the lab server (2026-11-16): the IPv6 route stalls forever and the IPv4 route
times out; one stalled download blocks the whole run. Those files only matter for *building*
environment images. We grade only instances whose instance image is already local (see
tasks.local_images), so the download is never needed: the two fetch functions return a placeholder.
Evaluation scripts, test commands and log parsing are untouched.
"""
import runpy

import swebench.harness.test_spec.python as _py

_PLACEHOLDER = "# not fetched: issue2patch grades only prebuilt instance images\n"


def _no_fetch(*_args, **_kwargs) -> str:
    return _PLACEHOLDER


_py.get_requirements = _no_fetch
_py.get_environment_yml = _no_fetch

if __name__ == "__main__":
    runpy.run_module("swebench.harness.run_evaluation", run_name="__main__", alter_sys=True)
