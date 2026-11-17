"""Node input/output contracts.

Nodes only see the fields declared here (decision 0001). Hidden tests and the
reference patch never appear in any of these models.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TaskSpec(BaseModel):
    """What the pipeline is allowed to know about a task."""

    task_id: str
    repo: str
    version: str
    base_commit: str
    issue: str
    image: str
    workdir: str = "/testbed"
    # Service tasks from a real repository (stage 2): the image holds an older checkout; this patch
    # (host file) brings it to the repository's current commit, whose tree must then match.
    setup_patch_file: str | None = None
    expected_tree: str | None = None
    source: str | None = None


class SandboxInfo(BaseModel):
    container: str
    image_id: str
    workdir: str
    head: str
    refs_removed: int


class Candidate(BaseModel):
    path: str
    score: float
    reasons: list[str]


class PrefetchOut(BaseModel):
    source_candidates: list[Candidate]
    test_candidates: list[Candidate]
    symbols: list[str]
    mentioned_paths: list[str]


class ReproOut(BaseModel):
    submitted: bool
    test_file: str | None = None
    command: str | None = None
    note: str = ""
    fails_on_original: bool | None = None  # deterministic check after the agent
    run_excerpt: str = ""
    reverted_files: list[str] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.submitted and self.test_file and self.command and self.fails_on_original)


class ImplementOut(BaseModel):
    submitted: bool
    summary: str = ""
    changed_files: list[str]


class CheckResult(BaseModel):
    name: str
    passed: bool
    skipped: bool = False
    detail: str = ""
    log_file: str | None = None  # host path, under the node directory
    sandbox_log: str | None = None  # same log inside the sandbox, for agents to read


class ValidateOut(BaseModel):
    passed: bool
    checks: list[CheckResult]


class RepairOut(BaseModel):
    rounds: int
    passed: bool
    validations: list[ValidateOut] = Field(default_factory=list)


class ReviewOut(BaseModel):
    submitted: bool
    verdict: Literal["approve", "request_changes", "none"] = "none"
    out_of_scope: list[str] = Field(default_factory=list)
    comments: str = ""
    reverted: bool = False


class PatchOut(BaseModel):
    files: list[str]
    excluded: list[str]
    empty: bool
    patch_file: str
    lines_added: int
    lines_removed: int
