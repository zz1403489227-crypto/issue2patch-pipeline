"""Closed-loop case 3: compare the implement node across runs that differ only in the edit tool.

usage: python scripts/edit_tool_ab.py --a cl3-nested-r1 --b cl3-flat-r1 [--base base-r1]
Per run: edit calls in the implement node, how many failed schema validation, how many failed otherwise
(e.g. oldText not found), whether the next file change after a failed edit succeeded, agent outcome,
requests, tokens, minutes, and implement one-shot (the first validation passes). Paired per task where
a metric is per task, with a 95% bootstrap interval over tasks.
"""
import argparse
import collections
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"


def load(p):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def node_dir(task_dir, node):
    hits = sorted(task_dir.glob(f"nodes/*-{node}"))
    return hits[0] if hits else None


def edits(events_file):
    """Edit calls in order: (ok, kind) with kind in ok / validation / other."""
    out, starts = [], {}
    for line in open(events_file, errors="replace"):
        if "tool_execution" not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev = e.get("event") or {}
        if ev.get("type") == "tool_execution_start":
            starts[ev.get("toolCallId")] = ev.get("toolName")
        if ev.get("type") != "tool_execution_end" or ev.get("toolName") not in ("edit", "write"):
            continue
        text = " ".join(c.get("text", "") for c in (ev.get("result") or {}).get("content", []) if isinstance(c, dict))
        kind = "ok" if not ev.get("isError") else ("validation" if text.startswith("Validation failed") else "other")
        out.append((ev["toolName"], kind))
    return out


def per_run(run):
    rows = {}
    for d in sorted((RUNS / run / "tasks").iterdir()):
        nd = node_dir(d, "implement")
        if nd is None or not (nd / "metrics.json").exists():
            continue
        m = load(nd / "metrics.json") or {}
        calls = []
        for ev in nd.glob("agent/*/events.jsonl"):
            calls += edits(ev)
        e_calls = [k for t, k in calls if t == "edit"]
        recover = []
        for i, (t, k) in enumerate(calls):
            if t == "edit" and k == "validation" and i + 1 < len(calls):
                recover.append(calls[i + 1][1] == "ok")
        vd = node_dir(d, "validate")
        chk = load(vd / "check.json") if vd else None
        runs = m.get("agent_runs") or [{}]
        rows[d.name] = {
            "edit_calls": len(e_calls),
            "edit_validation": sum(k == "validation" for k in e_calls),
            "edit_other_err": sum(k == "other" for k in e_calls),
            "write_calls": sum(t == "write" for t, _ in calls),
            "recover": recover,
            "status": runs[-1].get("status"),
            "tool_errors": sum(r.get("tool_errors", 0) for r in runs),
            "requests": (m.get("model") or {}).get("requests", 0),
            "tokens": (m.get("model") or {}).get("prompt_tokens", 0) + (m.get("model") or {}).get("completion_tokens", 0),
            "minutes": m.get("seconds", 0) / 60,
            "one_shot": None if chk is None else bool(chk.get("passed")),
        }
    return rows


def boot(diffs, n=2000, seed=0):
    rng = random.Random(seed)
    if not diffs:
        return None, None
    ms = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n)]


def summary(rows):
    c = collections.Counter()
    rec = []
    for r in rows.values():
        for k in ("edit_calls", "edit_validation", "edit_other_err", "write_calls", "tool_errors", "requests", "tokens"):
            c[k] += r[k]
        c["minutes"] += r["minutes"]
        c["submitted"] += r["status"] == "submitted"
        c["one_shot"] += bool(r["one_shot"])
        c["one_shot_n"] += r["one_shot"] is not None
        rec += r["recover"]
    n = len(rows)
    return {
        "tasks": n,
        "edit_calls": c["edit_calls"],
        "edit_validation_failures": c["edit_validation"],
        "edit_validation_rate": round(c["edit_validation"] / max(c["edit_calls"], 1), 4),
        "edit_other_errors": c["edit_other_err"],
        "next_change_ok_after_invalid_edit": f"{sum(rec)}/{len(rec)}",
        "write_calls": c["write_calls"],
        "submitted": round(c["submitted"] / max(n, 1), 3),
        "tool_errors_per_task": round(c["tool_errors"] / max(n, 1), 2),
        "requests_per_task": round(c["requests"] / max(n, 1), 1),
        "tokens_per_task": round(c["tokens"] / max(n, 1)),
        "minutes_per_task": round(c["minutes"] / max(n, 1), 2),
        "implement_one_shot": round(c["one_shot"] / max(c["one_shot_n"], 1), 3),
    }


ap = argparse.ArgumentParser()
ap.add_argument("--a", required=True)
ap.add_argument("--b", required=True)
ap.add_argument("--base", default=None)
args = ap.parse_args()
A, B = per_run(args.a), per_run(args.b)
out = {args.a: summary(A), args.b: summary(B)}
if args.base:
    out[args.base] = summary(per_run(args.base))
common = sorted(set(A) & set(B))
paired = {}
for name, fn in {
    "edit_validation_failures_per_task": lambda r: r["edit_validation"],
    "implement_one_shot": lambda r: None if r["one_shot"] is None else float(r["one_shot"]),
    "tokens_per_task": lambda r: r["tokens"],
    "tool_errors_per_task": lambda r: r["tool_errors"],
}.items():
    d = [fn(B[t]) - fn(A[t]) for t in common if fn(A[t]) is not None and fn(B[t]) is not None]
    lo, hi = boot(d)
    paired[name] = {"n": len(d), "mean_b_minus_a": round(sum(d) / len(d), 3) if d else None,
                    "ci95": [round(lo, 3), round(hi, 3)] if d else None}
out["paired_b_minus_a"] = paired
print(json.dumps(out, indent=1))
