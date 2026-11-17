"""Pi adapter: runs Pi 1.0.0 inside the task sandbox.

Node.js and Pi are bind-mounted read-only into the container. The node
configuration goes in on stdin, Pi events come out on stdout (one JSON per
line) and are stored under the node's trace directory on the host. Nothing is
written to a location that a later agent node could read.
"""
from __future__ import annotations

import json
import subprocess
import time

from ..config import Settings
from ..sandbox import AGENT_PATH
from .base import AgentRequest, AgentResult

GATEWAY_SOCKET_IN_SANDBOX = "/i2p/gw.sock"

QWEN_COMPAT = {
    # The Qwen3.6 model card controls thinking through chat_template_kwargs.enable_thinking and
    # recommends preserve_thinking for agents; Pi's "qwen-chat-template" format sends exactly these.
    # Pi's docs say to add other compat flags only for verified differences, so nothing else is set.
    "thinkingFormat": "qwen-chat-template",
}


class PiHarness:
    name = "pi"

    def __init__(self, settings: Settings):
        self.s = settings

    def run(self, req: AgentRequest) -> AgentResult:
        req.trace_dir.mkdir(parents=True, exist_ok=True)
        agent_dir = f"/tmp/i2p-agent/{req.node}-{int(time.time() * 1000)}"
        cfg = {
            "node": req.node,
            "model": self.s.model,
            "thinking": self.s.thinking,
            "context_window": self.s.context_window,
            "max_tokens": self.s.max_tokens,
            "request_timeout_s": self.s.request_timeout_s,
            "compat": QWEN_COMPAT,
            "finalize": self.s.finalize_turns,
            "edit_tool": self.s.edit_tool,
            "gateway_socket": GATEWAY_SOCKET_IN_SANDBOX,
            "cwd": req.workdir,
            "agent_dir": agent_dir,
            "tools": req.tools,
            "submit": req.submit,
            "max_requests": req.max_requests,
            "timeout_s": req.timeout_s,
            "system_prompt": req.system_prompt,
            "user_prompt": req.user_prompt,
        }
        (req.trace_dir / "system.md").write_text(req.system_prompt)
        (req.trace_dir / "prompt.md").write_text(req.user_prompt)
        (req.trace_dir / "config.json").write_text(json.dumps(
            {k: v for k, v in cfg.items() if k not in ("system_prompt", "user_prompt")}, indent=2))
        events = req.trace_dir / "events.jsonl"
        args = ["docker", "exec", "-i", "-w", req.workdir, "-e", f"PATH={AGENT_PATH}",
                "-e", "HOME=/root", req.container, "/opt/node/bin/node", "/opt/pi/runner.mjs"]
        t0 = time.time()
        hard_timeout = False
        with open(events, "w") as out, open(req.trace_dir / "stderr.log", "w") as err:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True)
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(cfg))
            proc.stdin.close()
            try:
                proc.wait(timeout=req.timeout_s + 180)
            except subprocess.TimeoutExpired:
                hard_timeout = True
                proc.kill()
                subprocess.run(["docker", "exec", req.container, "pkill", "-9", "-f", "runner.mjs"],
                               capture_output=True)
        subprocess.run(["docker", "exec", req.container, "rm", "-rf", agent_dir], capture_output=True)
        result = self._parse(events)
        result.seconds = time.time() - t0
        result.trace_file = str(events)
        if hard_timeout:
            result.status = "timeout"
            result.error = (result.error or "") + " host hard timeout"
        elif proc.returncode not in (0, None) and result.status == "error" and not result.error:
            result.error = f"runner exit {proc.returncode}: " + (req.trace_dir / "stderr.log").read_text()[-2000:]
        return result

    @staticmethod
    def _parse(events_path) -> AgentResult:
        submitted, finished = None, None
        with open(events_path, errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "submit":
                    submitted = ev.get("params")
                elif ev.get("type") == "agent_finished":
                    finished = ev
        if finished is None:
            return AgentResult(status="submitted" if submitted else "error", submitted=submitted,
                               error="runner output ended without agent_finished event")
        return AgentResult(
            status=finished.get("status", "error"),
            submitted=submitted,
            requests=finished.get("requests", 0),
            tool_calls=finished.get("tool_calls", 0),
            tool_errors=finished.get("tool_errors", 0),
            usage=finished.get("usage") or {},
            error=finished.get("error"),
            final_text=(finished.get("last_text") or "")[:4000],
        )
