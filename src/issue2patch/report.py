"""Per-run report: node funnel and cost table (tokens and time, per node)."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .state import NODE_ORDER


def _load(p: Path) -> Any:
    return json.loads(p.read_text()) if p.exists() else None


def gold_files(patch: str) -> set[str]:
    return set(re.findall(r"^diff --git a/(\S+) b/", patch, flags=re.M))


def _first_text(result: Any) -> str:
    if isinstance(result, dict):
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                return (c.get("text") or "").strip().splitlines()[0][:120] if c.get("text") else ""
    return str(result)[:120]


def tool_health(run_dir: Path) -> list[str]:
    """Appendix B #1: is tool calling with Pi + local Qwen stable? Counted from the traces."""
    t: dict[str, int] = defaultdict(int)
    err_kinds: dict[str, int] = defaultdict(int)
    for ev_file in run_dir.glob("tasks/*/nodes/*/agent/*/events.jsonl"):
        with open(ev_file, errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    t["unparseable_event_lines"] += 1
                    continue
                if e.get("type") == "agent_finished":
                    t[f"agent_status:{e.get('status')}"] += 1
                if e.get("type") != "pi_event":
                    continue
                ev = e["event"]
                if ev.get("type") == "tool_execution_end":
                    t["tool_calls"] += 1
                    if ev.get("isError"):
                        t["tool_errors"] += 1
                        msg = _first_text(ev.get("result"))
                        kind = "argument validation" if re.search(r"validat|invalid|schema|required", msg, re.I) \
                            else "unknown tool" if re.search(r"unknown tool|not found", msg, re.I) \
                            else "command/file error"
                        err_kinds[kind] += 1
                elif ev.get("type") == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
                    sr = ev["message"].get("stopReason")
                    t[f"assistant_stop:{sr}"] += 1
    for f in run_dir.glob("tasks/*/model_calls.jsonl"):
        for line in open(f):
            r = json.loads(line)
            t["model_requests"] += 1
            t[f"finish:{r.get('finish_reason')}"] += 1
            if (r.get("status") or 200) >= 400:
                t["http_errors"] += 1
    out = ["## Tool-call health (Pi + local Qwen)", "", "| counter | value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in sorted(t.items())]
    out += ["", "Tool errors by kind: " + (", ".join(f"{k} {v}" for k, v in err_kinds.items()) or "none"), ""]
    return out


def build(run_dir: Path, instances: dict[str, dict]) -> str:
    resolved = _load(run_dir / "grading" / "resolved.json") or {}
    rows, per_node = [], defaultdict(lambda: defaultdict(float))
    for task_dir in sorted((run_dir / "tasks").iterdir()):
        tid = task_dir.name
        st = _load(task_dir / "state.json") or {}
        row: dict[str, Any] = {"task": tid, "status": st.get("status"), "resolved": resolved.get(tid)}
        tot = defaultdict(float)
        for i, node in enumerate(NODE_ORDER, 1):
            nd = task_dir / "nodes" / f"{i}-{node}"
            m, c = _load(nd / "metrics.json") or {}, _load(nd / "check.json") or {}
            row[node] = "—" if not m else ("ok" if c.get("passed") else ("skip" if c.get("skipped") else "x"))
            mu = m.get("model") or {}
            for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "requests"):
                per_node[node][k] += mu.get(k, 0)
                tot[k] += mu.get(k, 0)
            per_node[node]["seconds"] += m.get("seconds", 0)
            per_node[node]["n"] += 1 if m else 0
            tot["seconds"] += m.get("seconds", 0)
        pre = _load(task_dir / "nodes" / "2-prefetch" / "output.json")
        if pre and tid in instances:
            gold = gold_files(instances[tid]["patch"])
            cands = {c["path"] for c in pre["source_candidates"]}
            row["prefetch_hit"] = bool(gold) and gold <= cands
        row.update({k: int(v) for k, v in tot.items() if k != "seconds"}, minutes=round(tot["seconds"] / 60, 1))
        rows.append(row)

    out = [f"# Run {run_dir.name}", "", "## Per task", ""]
    hdr = ["task", "status", *NODE_ORDER, "prefetch_hit", "resolved", "requests", "prompt_tokens",
           "completion_tokens", "reasoning_tokens", "minutes"]
    out += ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(h, "")) for h in hdr) + " |")
    out += ["", "## Cost per node (mean over tasks where the node ran)", "",
            "| node | runs | requests | prompt tokens | completion tokens | of which reasoning | minutes |",
            "|---|---|---|---|---|---|---|"]
    for node in NODE_ORDER:
        a = per_node[node]
        n = a["n"] or 1
        out.append(f"| {node} | {int(a['n'])} | {a['requests']/n:.1f} | {a['prompt_tokens']/n:,.0f} | "
                   f"{a['completion_tokens']/n:,.0f} | {a['reasoning_tokens']/n:,.0f} | {a['seconds']/n/60:.1f} |")
    n_tasks = max(len(rows), 1)
    tot_p = sum(per_node[x]["prompt_tokens"] for x in NODE_ORDER)
    tot_c = sum(per_node[x]["completion_tokens"] for x in NODE_ORDER)
    tot_s = sum(per_node[x]["seconds"] for x in NODE_ORDER)
    out += ["", f"Per task: {tot_p/n_tasks:,.0f} prompt tokens, {tot_c/n_tasks:,.0f} completion tokens, "
            f"{tot_s/n_tasks/60:.1f} minutes wall clock.", ""]
    out += tool_health(run_dir)
    return "\n".join(out)
