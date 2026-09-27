"""The meter: every model call the benchmark makes, counted and priced.

The throwaway services and the judge send their model calls here instead of
straight to the provider, by way of ``ANTHROPIC_BASE_URL`` and
``OPENAI_BASE_URL``. The meter runs on loopback inside the harness and does
one of two things with each call:

* **Paid runs** forward it to ``api.anthropic.com`` or ``api.openai.com``
  unchanged, stream the reply back, and read the token counts off the reply.
* **Mock runs** answer it here with a small canned reply in the provider's
  own format. Nothing leaves the computer and nothing is billed.

Each call becomes one row in ``usage.jsonl``: who made it, the model, the
token counts and the list-price cost. A row never holds a prompt, a reply or
a header. Once a paid run's spend reaches its budget, the meter refuses every
further call with an error the SDKs don't retry, so a run can't overspend by
more than the calls already in flight.

Only these two hosts are ever forwarded to, so the meter can't be used as an
open proxy.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

UPSTREAMS = {"anthropic": "https://api.anthropic.com",
             "openai": "https://api.openai.com"}

# $ per million tokens, list prices as of 27 September 2026. Anthropic
# prices match crossband's rate card. A dated model id is priced as its base
# model.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
    "gpt-4o-2024-08-06": (2.5, 10.0),
    "gpt-4o": (2.5, 10.0),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}
PRICES_AS_OF = "2026-09-27"
# Cache writes and reads, as a share of the input price.
ANTHROPIC_CACHE = {"write": 1.25, "read": 0.1}
OPENAI_CACHED_INPUT = 0.5
# An unknown model is costed at this rate, so a budget stays safe. The row
# says it was a guess.
FALLBACK_PRICE = (15.0, 75.0)

_DATED = re.compile(r"-(20\d{6}|\d{4}-\d{2}-\d{2})$")
_HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive",
               "transfer-encoding", "te", "trailer", "upgrade",
               "proxy-authorization", "proxy-authenticate", "accept-encoding"}


def price_for(model: str) -> tuple[tuple[float, float], bool]:
    """The model's (input, output) price and whether it's a known price."""
    model = model or ""
    for name in (model, _DATED.sub("", model)):
        if name in PRICES:
            return PRICES[name], True
    return FALLBACK_PRICE, False


def cost_of(provider: str, usage: dict) -> tuple[float, bool]:
    (p_in, p_out), known = price_for(usage.get("model", ""))
    fresh = usage.get("input", 0)
    cached = usage.get("cache_read", 0)
    written = usage.get("cache_write", 0)
    if provider == "anthropic":
        tokens_in = (fresh + written * ANTHROPIC_CACHE["write"]
                     + cached * ANTHROPIC_CACHE["read"])
    else:
        # OpenAI counts cached tokens inside the input total.
        tokens_in = (fresh - cached) + cached * OPENAI_CACHED_INPUT
    cost = (tokens_in * p_in + usage.get("output", 0) * p_out) / 1_000_000
    return round(cost, 8), known


# ---------------------------------------------------------------- usage parse

def _events(body: bytes):
    """Every JSON object in a reply: the whole body, or each SSE data line."""
    text = body.decode("utf-8", errors="replace")
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def parse_usage(provider: str, body: bytes) -> dict:
    """Token counts from a reply, streamed or not. Zeros when absent."""
    out = {"model": "", "input": 0, "output": 0, "cache_read": 0,
           "cache_write": 0}

    def keep(field, value):
        if isinstance(value, int) and value > out[field]:
            out[field] = value

    for ev in _events(body):
        if not isinstance(ev, dict):
            continue
        if provider == "anthropic":
            msg = ev.get("message") if ev.get("type") == "message_start" else ev
            if isinstance(msg, dict):
                out["model"] = msg.get("model") or out["model"]
                usage = msg.get("usage") or {}
            else:
                usage = {}
            if ev.get("type") == "message_delta":
                usage = ev.get("usage") or {}
            keep("input", usage.get("input_tokens"))
            keep("output", usage.get("output_tokens"))
            keep("cache_read", usage.get("cache_read_input_tokens"))
            keep("cache_write", usage.get("cache_creation_input_tokens"))
        else:
            holder = ev.get("response") if isinstance(ev.get("response"), dict) else ev
            out["model"] = holder.get("model") or out["model"]
            usage = holder.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            keep("input", usage.get("prompt_tokens"))
            keep("input", usage.get("input_tokens"))
            keep("output", usage.get("completion_tokens"))
            keep("output", usage.get("output_tokens"))
            details = (usage.get("prompt_tokens_details")
                       or usage.get("input_tokens_details") or {})
            keep("cache_read", details.get("cached_tokens"))
    return out


# ------------------------------------------------------------------ mock side

MOCK_ANSWER = "I don't have that information in my memory."


def _mock_text(prompt: str) -> str:
    """A reply shaped for whoever is asking, so every code path runs.

    The miner gets one well-formed fact, taken word for word from the first
    turn of its excerpt, so the membro grounding checks pass and the ledger
    fills. The judge gets "no". Everything else gets a fixed sentence.
    """
    if "permanent memory ledger" in prompt:
        m = re.search(r"\[msg 1\] [^:\n]+: ([^\n]{8,})", prompt)
        if not m:
            return "NONE"
        words = m.group(1).split()[:14]
        return f"NEW src=1 importance=5: User said {' '.join(words)}"
    if "Answer yes or no only." in prompt:
        return "no"
    return MOCK_ANSWER


def _prompt_text(payload: dict) -> str:
    def text_of(content):
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            return [b["text"] for b in content
                    if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return []

    parts = text_of(payload.get("system"))
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict):
            parts.extend(text_of(msg.get("content")))
    return "\n".join(parts)


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def mock_reply(provider: str, path: str, payload: dict) -> tuple[int, str, bytes]:
    """(status, content type, body) for one mocked call."""
    model = payload.get("model") or "mock"
    if provider == "anthropic" and path.endswith("/messages/count_tokens"):
        return 200, "application/json", json.dumps(
            {"input_tokens": _tokens(_prompt_text(payload))}).encode()
    if provider == "anthropic" and path.endswith("/messages"):
        prompt = _prompt_text(payload)
        text = _mock_text(prompt)
        usage = {"input_tokens": _tokens(prompt), "output_tokens": _tokens(text),
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if payload.get("stream"):
            events = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_mock", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {**usage, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start",
                                         "index": 0, "content_block": {
                                             "type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta",
                                         "index": 0, "delta": {
                                             "type": "text_delta", "text": text}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {
                    "stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": usage["output_tokens"]}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n"
                           for name, data in events)
            return 200, "text/event-stream", body.encode()
        return 200, "application/json", json.dumps({
            "id": "msg_mock", "type": "message", "role": "assistant",
            "model": model, "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": usage}).encode()
    if provider == "openai" and path.endswith("/embeddings"):
        inputs = payload.get("input")
        inputs = [inputs] if isinstance(inputs, str) else list(inputs or [])
        dims = int(payload.get("dimensions") or 1536)
        data = []
        for i, item in enumerate(inputs):
            seed = hashlib.sha256(str(item).encode()).digest()
            vec = [((seed[j % 32] + j) % 97) / 97.0 - 0.5 for j in range(dims)]
            if payload.get("encoding_format") == "base64":
                emb = base64.b64encode(struct.pack(f"<{dims}f", *vec)).decode()
            else:
                emb = vec
            data.append({"object": "embedding", "index": i, "embedding": emb})
        n = sum(_tokens(str(x)) for x in inputs)
        return 200, "application/json", json.dumps({
            "object": "list", "data": data, "model": model,
            "usage": {"prompt_tokens": n, "total_tokens": n}}).encode()
    if provider == "openai" and path.endswith("/chat/completions"):
        prompt = _prompt_text(payload)
        text = _mock_text(prompt)
        return 200, "application/json", json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": _tokens(prompt),
                      "completion_tokens": _tokens(text),
                      "total_tokens": _tokens(prompt) + _tokens(text)}}).encode()
    return 404, "application/json", json.dumps({"error": {
        "type": "not_found_error",
        "message": f"the bench_memory mock doesn't serve {path}"}}).encode()


# ---------------------------------------------------------------------- meter

class Meter:
    """The loopback server, its ledger of calls and the spend cap."""

    def __init__(self, usage_path: Path | str, *, mock: bool,
                 budget_usd: float | None = None, transport=None):
        self.usage_path = Path(usage_path)
        self.mock = mock
        self.budget_usd = budget_usd
        # A request's path names who sent it: "<key>.<service>". The runner
        # maps each key to its question and that question's current phase.
        self.questions: dict[str, str] = {}
        self.phases: dict[str, str] = {}
        self.spent = 0.0
        self.calls = 0
        self.refused = 0
        self.unpriced: set[str] = set()
        self._lock = threading.Lock()
        self._client = None if mock else httpx.Client(
            timeout=httpx.Timeout(600.0, connect=30.0), transport=transport)
        self._server = None
        self._thread = None
        self.url = ""
        self.spent = self._spent_so_far()

    def _spent_so_far(self) -> float:
        """A resumed run counts what earlier sessions already spent."""
        if not self.usage_path.exists():
            return 0.0
        total = 0.0
        for line in self.usage_path.read_text().splitlines():
            try:
                total += json.loads(line).get("cost_usd") or 0.0
            except json.JSONDecodeError:
                continue
        return total

    @property
    def over_budget(self) -> bool:
        return (not self.mock and self.budget_usd is not None
                and self.spent >= self.budget_usd)

    def start(self) -> str:
        meter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                meter._handle(self, "GET")

            def do_POST(self):
                meter._handle(self, "POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._client is not None:
            self._client.close()

    def record(self, tag: str, provider: str, status: int, usage: dict) -> None:
        cost, known = cost_of(provider, usage)
        if self.mock:
            cost = 0.0
        key, _, service = tag.partition(".")
        row = {"at": round(time.time(), 3), "service": service or key,
               "question_id": self.questions.get(key, ""),
               "phase": self.phases.get(key, key), "provider": provider,
               "status": status, **usage, "cost_usd": cost,
               "priced": known, "mock": self.mock}
        with self._lock:
            self.calls += 1
            self.spent += cost
            if not known and usage.get("model"):
                self.unpriced.add(usage["model"])
            with open(self.usage_path, "a") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    # One request: /<tag>/<provider>/<upstream path>
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = h.path.split("/", 3)
        if len(parts) < 4 or parts[2] not in UPSTREAMS:
            return self._send(h, 404, "application/json",
                              b'{"error": {"message": "unknown route"}}')
        tag, provider, rest = parts[1], parts[2], "/" + parts[3]
        length = int(h.headers.get("content-length") or 0)
        body = h.rfile.read(length) if length else b""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}

        if self.mock:
            status, ctype, reply = mock_reply(provider, rest.split("?")[0], payload)
            self.record(tag, provider, status, parse_usage(provider, reply)
                        if status == 200 else {"model": payload.get("model", "")})
            return self._send(h, status, ctype, reply)

        if self.over_budget:
            with self._lock:
                self.refused += 1
            return self._send(h, 400, "application/json", json.dumps({
                "type": "error", "error": {
                    "type": "invalid_request_error",
                    "message": "bench_memory: this run's budget is spent"}}).encode())

        headers = {k: v for k, v in h.headers.items()
                   if k.lower() not in _HOP_BY_HOP}
        headers["accept-encoding"] = "identity"
        url = UPSTREAMS[provider] + rest
        seen = bytearray()
        status, sent_headers = 599, False
        try:
            with self._client.stream(method, url, headers=headers,
                                     content=body or None) as resp:
                status = resp.status_code
                client_open = self._start_chunked(h, resp)
                sent_headers = True
                # Decoded bytes, since the encoding header isn't passed on.
                # Identity was asked for, so this is the raw reply unless a
                # provider compresses anyway.
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    seen.extend(chunk)
                    # A caller that hangs up mid-reply, as a service being
                    # stopped does, still gets its call counted: the reply is
                    # read to the end so the provider's token counts arrive.
                    if client_open:
                        client_open = self._write_chunk(h, chunk)
                if client_open:
                    self._write_chunk(h, b"")
        except httpx.HTTPError as exc:
            status = 599
            if not sent_headers:
                with contextlib.suppress(OSError):
                    self._send(h, 502, "application/json", json.dumps({"error": {
                        "type": "api_error",
                        "message": f"bench_memory meter: {type(exc).__name__}"}}).encode())
        usage = parse_usage(provider, bytes(seen))
        if not usage["model"]:
            usage["model"] = payload.get("model", "")
        self.record(tag, provider, status, usage)
        h.close_connection = True

    @staticmethod
    def _start_chunked(h, resp) -> bool:
        keep = {k: v for k, v in resp.headers.items()
                if k.lower() not in _HOP_BY_HOP
                and k.lower() not in ("content-encoding", "content-type")}
        try:
            h.send_response(resp.status_code)
            h.send_header("Content-Type",
                          resp.headers.get("content-type", "application/json"))
            for k, v in keep.items():
                h.send_header(k, v)
            h.send_header("Transfer-Encoding", "chunked")
            h.send_header("Connection", "close")
            h.end_headers()
            return True
        except OSError:
            return False

    @staticmethod
    def _write_chunk(h, chunk: bytes) -> bool:
        """One chunk of a chunked reply. An empty chunk ends the reply."""
        try:
            h.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            h.wfile.flush()
            return True
        except OSError:
            return False

    @staticmethod
    def _send(h, status: int, ctype: str, body: bytes) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(body)
        h.close_connection = True


def summarise(usage_path: Path | str) -> dict:
    """Spend and tokens from ``usage.jsonl``, by phase, service and model."""
    out = {"calls": 0, "cost_usd": 0.0, "by_phase": {}, "by_service": {},
           "by_model": {}, "by_question": {}, "unpriced": [], "errors": 0}
    path = Path(usage_path)
    if not path.exists():
        return out
    unpriced = set()
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        out["calls"] += 1
        cost = row.get("cost_usd") or 0.0
        out["cost_usd"] += cost
        if row.get("status", 200) >= 400:
            out["errors"] += 1
        if not row.get("priced", True) and row.get("model"):
            unpriced.add(row["model"])
        qid = row.get("question_id")
        if qid:
            out["by_question"][qid] = out["by_question"].get(qid, 0.0) + cost
        for key, name in (("by_phase", row.get("phase", "-")),
                          ("by_service", row.get("service", "-")),
                          ("by_model", row.get("model") or "-")):
            slot = out[key].setdefault(name, {"calls": 0, "cost_usd": 0.0,
                                              "input": 0, "output": 0,
                                              "cache_read": 0, "cache_write": 0})
            slot["calls"] += 1
            slot["cost_usd"] += cost
            for field in ("input", "output", "cache_read", "cache_write"):
                slot[field] += row.get(field) or 0
    out["unpriced"] = sorted(unpriced)
    return out
