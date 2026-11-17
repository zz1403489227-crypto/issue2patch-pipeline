from __future__ import annotations

from ..config import Settings
from .base import AgentRequest, AgentResult, Harness


def make_harness(settings: Settings) -> Harness:
    if settings.harness == "pi":
        from .pi import PiHarness

        return PiHarness(settings)
    raise ValueError(f"unknown harness {settings.harness!r}")


__all__ = ["AgentRequest", "AgentResult", "Harness", "make_harness"]
