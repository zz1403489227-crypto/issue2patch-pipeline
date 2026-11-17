"""Export runs to Langfuse (the dashboard), after the fact, from the files the pipeline writes.

The pipeline never talks to Langfuse: if the dashboard is down, nothing else is affected. Langfuse v4
ingests traces only over OpenTelemetry (OTLP/HTTP), so each task becomes one OTLP request built by
hand: that keeps the real start/end times and lets ids be deterministic, so re-exporting a run after
`evaluate` upserts instead of duplicating. Attribute names follow Langfuse's OTel mapping
(integrations/native/opentelemetry). Scores go to /api/public/scores.

Per task: a root span (trace name = task id, session = run id). Per node: a child span with the node's
contract input/output, check and metrics. Under agent nodes: one generation per model call (tokens,
latency, finish reason) and one tool span per tool call. Scores: `resolved` and every node's truth-based
`ok_<node>` from evaluation.json, which is what the funnel is built from.
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

from .state import NODE_ORDER

AGENT_NODES = {"reproduce", "implement", "repair", "review"}
MAX_STR = 20000


def _hex(n: int, *parts: str) -> str:
    return hashlib.sha256("/".join(parts).encode()).hexdigest()[: n * 2]


def _ns(t: float | None) -> str:
    return str(int((t or 0) * 1e9))


def _s(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= MAX_STR else s[:MAX_STR] + f"... [{len(s) - MAX_STR} chars clipped]"


def _attrs(d: dict[str, Any]) -> list[dict]:
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            val = {"boolValue": v}
        elif isinstance(v, int):
            val = {"intValue": str(v)}
        elif isinstance(v, float):
            val = {"doubleValue": v}
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            val = {"arrayValue": {"values": [{"stringValue": x} for x in v]}}
        else:
            val = {"stringValue": _s(v)}
        out.append({"key": k, "value": val})
    return out


def _load(p: Path) -> Any:
    return json.loads(p.read_text()) if p.exists() else None


def task_spans(run_id: str, task_dir: Path, evaluation: dict | None) -> tuple[str, list[dict], list[dict]]:
    tid = task_dir.name
    trace_id = _hex(16, run_id, tid)
    st = _load(task_dir / "state.json") or {}
    ev = [json.loads(x) for x in (task_dir / "events.jsonl").read_text().splitlines()] \
        if (task_dir / "events.jsonl").exists() else []
    spans: list[dict] = []
    e = (evaluation or {}).get("tasks", {}).get(tid, {})

    def span(span_id, parent, name, start, end, attrs, error=False):
        spans.append({"traceId": trace_id, "spanId": span_id, "parentSpanId": parent or "", "name": name,
                      "kind": 1, "startTimeUnixNano": _ns(start), "endTimeUnixNano": _ns(end or start),
                      "attributes": _attrs(attrs), "status": {"code": 2 if error else 1}})

    root = _hex(8, run_id, tid, "root")
    first_input = _load(task_dir / "nodes" / "1-prepare" / "input.json") or {}
    span(root, None, tid, st.get("created_at"), st.get("finished_at") or st.get("updated_at"), {
        "langfuse.observation.type": "chain",
        "langfuse.trace.name": tid, "langfuse.session.id": run_id,
        "langfuse.trace.tags": [run_id, f"status:{st.get('status')}"]
        + ([f"resolved:{e['resolved']}"] if "resolved" in e else [])
        + ([f"failed_at:{e['attributed_to']}"] if e.get("attributed_to") else []),
        "langfuse.trace.metadata.status": st.get("status"),
        "langfuse.trace.metadata.resolved": str(e.get("resolved")),
        "langfuse.trace.metadata.attributed_to": e.get("attributed_to"),
        "langfuse.observation.input": (first_input.get("task") or {}).get("issue"),
        "langfuse.observation.output": {"status": st.get("status"), "summary": st.get("summary")},
    }, error=st.get("status") == "infra_error")
    node_span: dict[str, str] = {}
    for i, node in enumerate(NODE_ORDER, 1):
        nd = task_dir / "nodes" / f"{i}-{node}"
        if not nd.exists():
            continue
        starts = [x["t"] for x in ev if x["kind"] == "node_started" and x.get("node") == node]
        ends = [x["t"] for x in ev if x["kind"] == "node_finished" and x.get("node") == node]
        check = _load(nd / "check.json")
        status = ((st.get("nodes") or {}).get(node) or {}).get("status")
        sid = node_span[node] = _hex(8, run_id, tid, node)
        failed = check is not None and check.get("passed") is False
        span(sid, root, f"{i}-{node}", starts[-1] if starts else None, ends[-1] if ends else None, {
            "langfuse.observation.type": "agent" if node in AGENT_NODES else "chain",
            "langfuse.observation.input": _load(nd / "input.json"),
            "langfuse.observation.output": _load(nd / "output.json"),
            "langfuse.observation.level": "ERROR" if status == "error" else ("WARNING" if failed else "DEFAULT"),
            "langfuse.observation.status_message": (nd / "error.txt").read_text()[-2000:]
            if (nd / "error.txt").exists() else None,
            "langfuse.observation.metadata.check": check,
            "langfuse.observation.metadata.metrics": _load(nd / "metrics.json"),
            "langfuse.observation.metadata.status": status,
            "langfuse.observation.metadata.attempts": len(starts),
        }, error=status == "error")
        for trace_file in sorted((nd / "agent").glob("*/events.jsonl")) if (nd / "agent").exists() else []:
            pending: dict[str, dict] = {}
            for line in trace_file.read_text(errors="replace").splitlines():
                pe = json.loads(line)
                if pe.get("type") != "pi_event":
                    continue
                x = pe["event"]
                if x.get("type") == "tool_execution_start":
                    pending[x.get("toolCallId")] = {"t": pe["t"] / 1000, "args": x.get("args")}
                elif x.get("type") == "tool_execution_end":
                    s0 = pending.pop(x.get("toolCallId"), {})
                    res = x.get("result") or {}
                    text = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
                    span(_hex(8, run_id, tid, node, trace_file.parent.name, str(x.get("toolCallId"))), sid,
                         f"tool:{x.get('toolName')}", s0.get("t"), pe["t"] / 1000, {
                             "langfuse.observation.type": "tool",
                             "langfuse.observation.input": s0.get("args"),
                             "langfuse.observation.output": text[:4000],
                             "langfuse.observation.level": "WARNING" if x.get("isError") else "DEFAULT",
                         })
    calls = task_dir / "model_calls.jsonl"
    for k, line in enumerate(calls.read_text().splitlines() if calls.exists() else []):
        r = json.loads(line)
        u = r.get("usage") or {}
        t0 = r.get("started") or 0
        span(_hex(8, run_id, tid, "call", str(k)), node_span.get(r.get("node"), root), f"{r.get('node')}:model",
             t0, t0 + (r.get("seconds") or 0), {
                 "langfuse.observation.type": "generation",
                 "langfuse.observation.model.name": r.get("model"),
                 "langfuse.observation.usage_details": {
                     "input": u.get("prompt_tokens") or 0, "output": u.get("completion_tokens") or 0,
                     "cache_read_input_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
                     "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0},
                 "langfuse.observation.level": "ERROR" if (r.get("status") or 200) >= 400 else "DEFAULT",
                 "langfuse.observation.metadata.finish_reason": r.get("finish_reason"),
                 "langfuse.observation.metadata.messages": r.get("messages"),
                 "langfuse.observation.metadata.ttft_s": r.get("ttft"),
             }, error=(r.get("status") or 200) >= 400)
    scores = []
    if "resolved" in e:
        scores.append({"id": _hex(16, run_id, tid, "score", "resolved"), "traceId": trace_id, "name": "resolved",
                       "value": 1 if e["resolved"] else 0, "dataType": "BOOLEAN"})
    for node, ok in (e.get("ok") or {}).items():
        scores.append({"id": _hex(16, run_id, tid, "score", node), "traceId": trace_id, "name": f"ok_{node}",
                       "value": 1 if ok else 0, "dataType": "BOOLEAN", "observationId": node_span.get(node)})
    return trace_id, spans, scores


def _post(url: str, body: dict, auth: str, extra: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {auth}", **(extra or {})}
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def export_run(run_dir: Path, host: str, public_key: str, secret_key: str) -> int:
    evaluation = _load(run_dir / "evaluation.json")
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    host = host.rstrip("/")
    n = 0
    for task_dir in sorted(d for d in (run_dir / "tasks").iterdir() if d.is_dir()):
        _, spans, scores = task_spans(run_dir.name, task_dir, evaluation)
        body = {"resourceSpans": [{"resource": {"attributes": _attrs({"service.name": "issue2patch"})},
                                   "scopeSpans": [{"scope": {"name": "issue2patch.export"}, "spans": spans}]}]}
        res = _post(f"{host}/api/public/otel/v1/traces", body, auth, {"x-langfuse-ingestion-version": "4"})
        if (res.get("partialSuccess") or {}).get("rejectedSpans"):
            raise RuntimeError(f"Langfuse rejected spans for {task_dir.name}: {res['partialSuccess']}")
        for sc in scores:
            _post(f"{host}/api/public/scores", {k: v for k, v in sc.items() if v is not None}, auth)
        n += len(spans)
    return n
