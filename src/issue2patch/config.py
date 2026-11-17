"""Runtime settings. Everything tunable lives here so a run can record it verbatim."""
from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("I2P_ROOT", Path(__file__).resolve().parents[2]))
VAR = ROOT / "var"


def _load_local_env() -> None:
    """Machine-specific values (e.g. I2P_NODE_HOME) from var/local.env, which is not in git.
    Lines are KEY=VALUE; variables already set in the environment win."""
    p = VAR / "local.env"
    if p.exists():
        for line in p.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def _node_home() -> str:
    """Node.js install that is mounted read-only into task containers: I2P_NODE_HOME, else the `node` on PATH."""
    if os.environ.get("I2P_NODE_HOME"):
        return os.environ["I2P_NODE_HOME"]
    node = shutil.which("node")
    return str(Path(node).resolve().parents[1]) if node else ""


_load_local_env()


def _limits() -> dict[str, dict[str, int]]:
    # max_requests: model requests per agent node; timeout_s: wall clock per node.
    return {
        "reproduce": {"max_requests": 40, "timeout_s": 1500},
        "implement": {"max_requests": 60, "timeout_s": 2100},
        "repair": {"max_requests": 30, "timeout_s": 1200},
        "review": {"max_requests": 20, "timeout_s": 900},
        "smoke": {"max_requests": 6, "timeout_s": 300},
    }


@dataclass
class Settings:
    vllm_url: str = os.environ.get("I2P_VLLM_URL", "http://127.0.0.1:18949")
    model: str = "qwen3.6-35b-a3b"
    thinking: str = "medium"  # decision 2026-11-15: thinking mode fixed ON
    context_window: int = 131072
    max_tokens: int = 16384
    request_timeout_s: int = int(os.environ.get("I2P_REQUEST_TIMEOUT_S", "900"))
    harness: str = "pi"
    node_home: str = field(default_factory=_node_home)
    pi_dir: str = str(ROOT / "harness" / "pi")
    tools_dir: str = str(VAR / "tools")
    runs_dir: str = os.environ.get("I2P_RUNS_DIR", str(ROOT / "runs"))
    sock_root: str = str(VAR / "sock")
    data_file: str = str(VAR / "data" / "swebench_verified.jsonl")
    container_cpus: str = "4"
    container_memory: str = "16g"
    test_timeout_s: int = 900
    repair_rounds: int = 2
    # Timeouts, three layers: one model call (gateway), one node (agent_limits / test timeouts), one task.
    task_timeout_s: int = 3600
    # Budget per task: prompt + completion tokens through the gateway. Stage 0 max was 3.0M (django-11433).
    task_token_budget: int = int(os.environ.get("I2P_TASK_TOKEN_BUDGET", "6000000"))
    # Retry class 1 (infrastructure errors): same node again after restoring its starting snapshot.
    node_max_attempts: int = 3
    retry_backoff_s: float = 10.0
    keep_containers: bool = False
    # Closed-loop case 1: switch off every tool but submit for the last 3 turns of an agent node.
    # False reproduces the baseline (a steer message only). Env override for paired comparisons.
    finalize_turns: bool = os.environ.get("I2P_FINALIZE", "1") == "1"
    # Closed-loop case 3: "flat" = edit(path, oldText, newText); anything else (e.g. "nested") = Pi's edit(path, edits[]).
    edit_tool: str = os.environ.get("I2P_EDIT_TOOL", "nested")
    agent_limits: dict[str, dict[str, int]] = field(default_factory=_limits)

    def to_dict(self) -> dict:
        return asdict(self)
