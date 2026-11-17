"""Smoke-test the vLLM endpoint: thinking on, one tool call, one tool result round trip."""
import json
import sys
import time
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18949"
MODEL = "qwen3.6-35b-a3b"
TOOLS = [{"type": "function", "function": {
    "name": "run_tests", "description": "Run a test file and return the output.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def chat(messages, **kw):
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "max_tokens": 4096,
            "chat_template_kwargs": {"enable_thinking": True}, **kw}
    req = urllib.request.Request(URL + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read())
    return out, time.time() - t0


msgs = [{"role": "user", "content": "Run the tests in tests/test_math.py and tell me whether they pass."}]
out, dt = chat(msgs)
m = out["choices"][0]["message"]
calls = m.get("tool_calls") or []
reasoning = m.get("reasoning_content") or m.get("reasoning") or ""
print(f"call1 {dt:.1f}s finish={out['choices'][0]['finish_reason']} reasoning_chars={len(reasoning)} "
      f"tool_calls={[(c['function']['name'], c['function']['arguments']) for c in calls]} usage={out['usage']}")
assert calls and calls[0]["function"]["name"] == "run_tests", "no tool call"
json.loads(calls[0]["function"]["arguments"])
msgs += [{"role": "assistant", "content": m.get("content") or "", "tool_calls": calls},
         {"role": "tool", "tool_call_id": calls[0]["id"], "content": "2 passed, 1 failed: test_div (ZeroDivisionError)"}]
out, dt = chat(msgs)
m = out["choices"][0]["message"]
print(f"call2 {dt:.1f}s finish={out['choices'][0]['finish_reason']} content={(m.get('content') or '')[:200]!r} usage={out['usage']}")
print("SMOKE_OK")
