"""The one interface agent nodes use to call a harness (decision 0003).

A node describes *what* it needs (prompts, tools, a structured submit schema,
limits); the adapter decides *how* (Pi today, OpenCode later). Swapping the
harness changes `Settings.harness`, nothing in the nodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

# Field spec for the structured "submit" tool: name -> {type, description, values?, optional?}
# type is one of: "string", "enum", "string_array".
SubmitSpec = dict[str, Any]

AgentStatus = Literal["submitted", "finished_without_submit", "budget_exhausted", "timeout", "error"]


@dataclass
class AgentRequest:
    node: str
    system_prompt: str
    user_prompt: str
    container: str
    workdir: str
    trace_dir: Path
    tools: list[str]
    submit: SubmitSpec | None
    max_requests: int
    timeout_s: int


@dataclass
class AgentResult:
    status: AgentStatus
    submitted: dict[str, Any] | None
    requests: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    error: str | None = None
    final_text: str = ""
    trace_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Harness(Protocol):
    name: str

    def run(self, req: AgentRequest) -> AgentResult: ...
