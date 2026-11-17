"""What a node receives from the orchestrator."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..config import Settings
from ..contracts import TaskSpec
from ..gateway import ModelGateway
from ..harness import AgentRequest, AgentResult, Harness
from ..sandbox import DockerSandbox, InfraError
from ..state import StateStore
from ..testing import RepoTestInfo, is_test_path

# Agent errors that mean the plumbing failed rather than the model: connection to the gateway or
# vLLM, 5xx from upstream, the runner process dying.
INFRA_SIGNS = re.compile(r"ECONN|EPIPE|socket hang up|fetch failed|Connection error|5\d\d |gateway:|"
                         r"runner exit|without agent_finished", re.I)


class StopTask(Exception):
    """The task's time or token budget is used up: stop agent work, wrap up, record why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class TaskContext:
    run_id: str
    task: TaskSpec
    settings: Settings
    store: StateStore
    harness: Harness
    tests: RepoTestInfo
    sock_dir: Path
    gateway: ModelGateway | None = None
    sandbox: DockerSandbox | None = None
    outputs: dict[str, BaseModel] = field(default_factory=dict)
    agent_runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    deadline: float = float("inf")  # timeout layer 3: the whole task

    @property
    def sb(self) -> DockerSandbox:
        assert self.sandbox is not None, "sandbox not prepared"
        return self.sandbox

    def node_dir(self, node: str) -> Path:
        return self.store.node_dir(self.task.task_id, node)

    def repro_file(self) -> str | None:
        r = self.outputs.get("reproduce")
        return getattr(r, "test_file", None) if r is not None else None

    def repro_paths(self) -> set[str]:
        """The repro test file and any helper files the reproduce node legitimately created."""
        return set(self.cache.get("repro_paths", []))

    def source_changes(self) -> tuple[list[str], list[str]]:
        """(source files changed, files excluded from the patch)."""
        changed = self.sb.changed_files()
        tracked_changed = set(self.sb.git("ls-files -m").output.splitlines())
        repro = self.repro_paths()
        src = [f for f in changed
               if f not in repro and not is_test_path(f)
               # untracked non-Python files are build/test by-products, never part of a fix
               and (f in tracked_changed or f.endswith(".py"))]
        excluded = [f for f in changed if f not in src]
        return src, excluded

    def remaining_s(self) -> float:
        return self.deadline - time.time()

    def check_budget(self) -> None:
        """Raise StopTask when the task's time or token budget is used up (checked before agent work)."""
        if self.remaining_s() < 60:
            raise StopTask("timeout")
        if self.gateway and self.gateway.total_tokens() >= self.settings.task_token_budget:
            raise StopTask("budget_exhausted")

    def run_agent(self, node: str, system: str, prompt: str, tools: list[str], submit: dict | None,
                  label: str | None = None, limits: dict[str, int] | None = None) -> AgentResult:
        self.check_budget()
        limits = limits or self.settings.agent_limits.get(node, {"max_requests": 30, "timeout_s": 900})
        base = self.node_dir(node) / "agent" / (label or "run")
        trace, n = base, 1
        while trace.exists():  # a retried attempt keeps the earlier trace
            n += 1
            trace = base.with_name(f"{base.name}-attempt{n}")
        prompt += (f"\n\nBudget: at most {limits['max_requests']} model turns for this step. "
                   "Call submit before you run out; unsubmitted work is discarded.")
        timeout = int(min(limits["timeout_s"], self.remaining_s()))  # timeout layer 2, capped by layer 3
        res = self.harness.run(AgentRequest(
            node=node, system_prompt=system, user_prompt=prompt, container=self.sb.container,
            workdir=self.task.workdir, trace_dir=trace, tools=tools, submit=submit,
            max_requests=limits["max_requests"], timeout_s=timeout))
        self.agent_runs.setdefault(node, []).append({"label": trace.name, **res.to_dict()})
        self.store.event(self.task.task_id, "agent_finished", node=node, label=trace.name, status=res.status,
                         requests=res.requests, tool_calls=res.tool_calls, tool_errors=res.tool_errors)
        if res.status == "error":
            err = res.error or ""
            if "budget_exhausted" in err or "token budget" in err:
                raise StopTask("budget_exhausted")
            if INFRA_SIGNS.search(err) or not self.sb.is_running():
                raise InfraError(f"agent run failed on infrastructure: {err[:500]}")
        return res

    def put_log(self, node: str, name: str, text: str) -> tuple[str, str]:
        """Write a log on the host node dir and into the sandbox; return (host_path, sandbox_path)."""
        host = self.node_dir(node) / f"{name}.log"
        host.write_text(text)
        inside = f"/tmp/i2p/logs/{node}-{name}.log"
        self.sb.write_file(inside, text)
        return str(host), inside


def tail(text: str, n: int = 3000) -> str:
    return text if len(text) <= n else "...[truncated]...\n" + text[-n:]
