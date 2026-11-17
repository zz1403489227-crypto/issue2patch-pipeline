"""Model gateway: the only path from a sandbox to the model.

The sandbox has no network. The agent connects to a Unix socket mounted into
the container; this gateway forwards to vLLM and records one line per request
(node, tokens, latency, finish reason). Stage 1 adds budgets and timeouts here,
stage 2 adds rate limiting and priorities.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web


class ModelGateway:
    def __init__(self, sock_path: Path, upstream: str, log_path: Path, task_id: str,
                 request_timeout_s: float = 900, token_budget: int | None = None, used_tokens: int = 0):
        self.sock_path = Path(sock_path)
        self.upstream = upstream.rstrip("/")
        self.log_path = Path(log_path)
        self.task_id = task_id
        self.request_timeout_s = request_timeout_s  # timeout layer 1: one model call
        self.token_budget = token_budget  # backstop for the per-task budget; the orchestrator stops first
        self.used_tokens = used_tokens  # carried over when a task is resumed
        self.current_node = "unknown"
        self.usage: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._serve, name=f"gw-{self.task_id}", daemon=True)
        self._thread.start()
        if not self._ready.wait(30) or self._error:
            raise RuntimeError(f"gateway failed to start: {self._error!r}")

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start())
        except BaseException as e:  # surfaced to start()
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        loop.run_forever()

    async def _start(self) -> None:
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10, sock_read=900))
        app = web.Application(client_max_size=128 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        if self.sock_path.exists():
            self.sock_path.unlink()
        await web.UnixSite(self._runner, str(self.sock_path)).start()
        os.chmod(self.sock_path, 0o666)

    def stop(self) -> None:
        if not self._loop:
            return
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            fut.result(30)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(10)
            if self.sock_path.exists():
                self.sock_path.unlink()

    async def _shutdown(self) -> None:
        await self._runner.cleanup()
        await self._session.close()

    # fault injection (stage 3) ------------------------------------------------
    def _fault(self, payload: dict[str, Any] | None) -> str | None:
        """I2P_FAULT="<kind>@<n>[,<kind>@<n>...]": inject <kind> into the n-th chat request of each task.
        Kinds: model_timeout (the request hangs past the gateway's timeout), bad_tool_call (the model
        "returns" a tool call whose arguments are truncated JSON), bad_tool_args (valid JSON that does not
        match the tool's schema: `cmd` instead of the required `command`)."""
        spec = os.environ.get("I2P_FAULT", "")
        if not spec or payload is None:
            return None
        with self._lock:
            self._n_chat = getattr(self, "_n_chat", 0) + 1
            n = self._n_chat
        for item in spec.split(","):
            kind, _, at = item.partition("@")
            if at.isdigit() and int(at) == n:
                return kind.strip()
        return None

    async def _fake_bad_tool_call(self, request, payload, rec, t0, arguments: str) -> web.StreamResponse:
        resp = web.StreamResponse(status=200)
        resp.content_type = "text/event-stream"
        await resp.prepare(request)
        base = {"id": "chatcmpl-fault", "object": "chat.completion.chunk", "created": int(t0),
                "model": payload.get("model")}
        chunks = [
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": "call_fault", "type": "function",
                 "function": {"name": "bash", "arguments": arguments}}]}, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
        ]
        for c in chunks:
            await resp.write(b"data: " + json.dumps(c).encode() + b"\n\n")
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        rec.update(status=200, finish_reason="tool_calls", seconds=time.time() - t0)
        self._record(rec)
        return resp

    # accounting --------------------------------------------------------------
    def node_usage(self, node: str) -> dict[str, float]:
        with self._lock:
            return dict(self.usage.get(node, {}))

    def total_tokens(self) -> int:
        with self._lock:
            return self.used_tokens

    def _record(self, rec: dict[str, Any]) -> None:
        u = rec.get("usage") or {}
        with self._lock:
            self.used_tokens += (u.get("prompt_tokens", 0) or 0) + (u.get("completion_tokens", 0) or 0)
            agg = self.usage[rec["node"]]
            agg["requests"] += 1
            agg["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
            agg["completion_tokens"] += u.get("completion_tokens", 0) or 0
            agg["cached_tokens"] += ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            agg["reasoning_tokens"] += ((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            agg["reasoning_chars"] += rec.get("reasoning_chars", 0)
            agg["model_seconds"] += rec.get("seconds", 0.0)
            if rec.get("status", 200) >= 400:
                agg["errors"] += 1
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def _scan_sse(buf: bytes, rec: dict[str, Any]) -> bytes:
        *lines, rest = buf.split(b"\n")
        for line in lines:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("usage"):
                rec["usage"] = obj["usage"]
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    rec["finish_reason"] = ch["finish_reason"]
                delta = ch.get("delta") or {}
                r = delta.get("reasoning_content") or delta.get("reasoning") or ""
                rec["reasoning_chars"] = rec.get("reasoning_chars", 0) + len(r)
                rec["content_chars"] = rec.get("content_chars", 0) + len(delta.get("content") or "")
                if delta.get("tool_calls"):
                    rec["tool_call_deltas"] = rec.get("tool_call_deltas", 0) + 1
        return rest

    # proxy -------------------------------------------------------------------
    async def _handle(self, request: web.Request) -> web.StreamResponse:
        t0 = time.time()
        rec: dict[str, Any] = {"task": self.task_id, "node": self.current_node, "method": request.method,
                               "path": request.path, "started": t0}
        body = await request.read()
        payload: dict[str, Any] | None = None
        if body and request.content_type == "application/json":
            payload = json.loads(body)
            if payload.get("stream"):
                payload.setdefault("stream_options", {})["include_usage"] = True
            rec.update(messages=len(payload.get("messages") or []), tools=len(payload.get("tools") or []),
                       max_tokens=payload.get("max_tokens") or payload.get("max_completion_tokens"),
                       stream=bool(payload.get("stream")),
                       chat_template_kwargs=payload.get("chat_template_kwargs"),
                       # what the client actually sends, so settings can be checked against the docs
                       keys=sorted(payload.keys()),
                       roles=sorted({m.get("role") for m in payload.get("messages") or []}),
                       sampling={k: payload[k] for k in ("temperature", "top_p", "top_k", "presence_penalty",
                                                         "min_p", "repetition_penalty") if k in payload})
            body = json.dumps(payload).encode()
        headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
        resp: web.StreamResponse | None = None
        if self.token_budget is not None and self.total_tokens() >= self.token_budget and payload is not None:
            rec.update(status=429, error="task token budget exhausted", seconds=0.0)
            self._record(rec)
            return web.json_response({"error": {"message": "task token budget exhausted",
                                                "type": "budget_exhausted"}}, status=429)
        fault = self._fault(payload)
        if fault in ("bad_tool_call", "bad_tool_args"):
            rec.update(fault=fault)
            return await self._fake_bad_tool_call(request, payload, rec, t0,
                                                  '{"command": "ls -la' if fault == "bad_tool_call" else '{"cmd": "ls"}')
        try:
            if fault == "model_timeout":
                rec.update(fault=fault)
                # the upstream call itself is fine; the gateway's own timeout layer must fire
                async with asyncio.timeout(self.request_timeout_s):
                    await asyncio.sleep(self.request_timeout_s + 5)
            async with self._session.request(request.method, self.upstream + request.path_qs,
                                             data=body or None, headers=headers,
                                             timeout=ClientTimeout(total=self.request_timeout_s,
                                                                   sock_connect=10)) as up:
                resp = web.StreamResponse(status=up.status)
                resp.content_type = up.content_type
                await resp.prepare(request)
                tail, first, whole = b"", None, bytearray()
                async for chunk in up.content.iter_any():
                    if first is None:
                        first = time.time()
                    await resp.write(chunk)
                    if rec.get("stream"):
                        tail = self._scan_sse(tail + chunk, rec)
                    elif len(whole) < 8 * 1024 * 1024:
                        whole.extend(chunk)
                if rec.get("stream"):
                    self._scan_sse(tail + b"\n", rec)
                else:
                    try:
                        obj = json.loads(bytes(whole))
                        rec["usage"] = obj.get("usage")
                        rec["finish_reason"] = (obj.get("choices") or [{}])[0].get("finish_reason")
                    except ValueError:
                        pass
                if up.status >= 400:
                    rec["error_body"] = bytes(whole[:2000]).decode(errors="replace")
                await resp.write_eof()
                rec.update(status=up.status, ttft=(first - t0) if first else None)
                return resp
        except asyncio.TimeoutError:
            rec.update(status=504, error=f"model call exceeded {self.request_timeout_s}s")
            if resp is None:
                return web.json_response({"error": {"message": rec["error"], "type": "timeout"}}, status=504)
            raise
        except Exception as e:  # upstream unreachable, reset, ...
            rec.update(status=599, error=repr(e))
            if resp is None:
                return web.json_response({"error": {"message": f"gateway: {e!r}"}}, status=502)
            raise
        finally:
            rec["seconds"] = time.time() - t0
            self._record(rec)
