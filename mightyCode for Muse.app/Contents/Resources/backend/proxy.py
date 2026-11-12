"""LocalCode API proxy: Anthropic Messages API -> OpenAI Chat Completions.

Claude Code (the real CLI) talks to this proxy as if it were the Anthropic
API (ANTHROPIC_BASE_URL=http://127.0.0.1:18081). The proxy translates each
request to the OpenAI-compatible API of the local mlx_lm.server and
translates the response back, including SSE streaming.

Written from scratch for LocalCode (the translation approach is the one
popularised by shi3z/mightyCode; no mightyCode code is used — it ships
without a licence).

Extras:
- The requested model name is ignored; every call goes to UPSTREAM_MODEL.
- EoCC-style compaction: when a tool-using conversation grows past
  COMPACT_THRESHOLD estimated tokens, the oldest middle turns are replaced
  by a recap the model writes itself (task + recap + recent turns are
  kept), so the small local model can run long sessions in a small window.

Environment:
  PROXY_PORT        listen port (default 18081)
  UPSTREAM_BASE     OpenAI-compatible base (default http://127.0.0.1:18080/v1)
  UPSTREAM_MODEL    model id sent upstream (default "local-model")
  COMPACT_THRESHOLD estimated-token threshold for EoCC (default 48000, 0=off)
  RECENT_BUDGET     estimated tokens of recent turns kept verbatim (12000)
  PROXY_DEBUG_LOG   optional path; appends one JSON line per request
"""
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PROXY_PORT", "18081"))
UPSTREAM = os.environ.get("UPSTREAM_BASE", "http://127.0.0.1:18080/v1").rstrip("/")
UPSTREAM_MODEL = os.environ.get("UPSTREAM_MODEL", "local-model")
THRESHOLD = int(os.environ.get("COMPACT_THRESHOLD", "48000"))
RECENT_BUDGET = int(os.environ.get("RECENT_BUDGET", "12000"))
DEBUG_LOG = os.environ.get("PROXY_DEBUG_LOG", "")

RECAP_INSTRUCTION = (
    "Summarize the following partial conversation from a coding session so "
    "that work can continue from your summary alone. Include: the objective, "
    "established facts, files read or changed and how, commands run and their "
    "outcomes, dead ends, and the next step. Be specific and compact.\n\n"
)

_lock = threading.Lock()
_recap_cache = {}

# Request counters for LocalCode's health checks (GET /stats).
STATS = {"started_at": time.time(), "messages": 0, "probes": 0,
         "last_message_ts": 0.0, "upstream_ok": 0, "upstream_err": 0,
         "last_upstream_ok_ts": 0.0, "last_upstream_err": "",
         "inflight": 0, "last_chunk_ts": 0.0}


def _stat(**kw):
    with _lock:
        for k, v in kw.items():
            if k in ("messages", "probes", "upstream_ok", "upstream_err", "inflight"):
                STATS[k] += v
            else:
                STATS[k] = v


def debug_log(obj):
    if not DEBUG_LOG:
        return
    try:
        with _lock:
            with open(DEBUG_LOG, "a") as f:
                f.write(json.dumps(obj, ensure_ascii=False)[:20000] + "\n")
    except OSError:
        pass


def estimate_tokens(obj):
    return int(len(json.dumps(obj, ensure_ascii=False)) / 2.2)


# --------------------------------------------------------------------------
# Anthropic -> OpenAI
# --------------------------------------------------------------------------

def _text_of_blocks(content):
    """Flatten Anthropic content (str | blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_result":
            parts.append(_text_of_blocks(b.get("content")))
        elif t == "image":
            parts.append("[image omitted]")
    return "\n".join(p for p in parts if p)


def anthropic_to_openai(req):
    msgs = []
    system = req.get("system")
    if system:
        text = _text_of_blocks(system)
        if text:
            msgs.append({"role": "system", "content": text})
    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        blocks = content or []
        if role == "user":
            texts = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "tool_result":
                    msgs.append({"role": "tool",
                                 "tool_call_id": b.get("tool_use_id", ""),
                                 "content": _text_of_blocks(b.get("content"))})
                elif t == "text":
                    texts.append(b.get("text", ""))
                elif t == "image":
                    texts.append("[image omitted: the local model has no vision]")
                else:
                    texts.append(_text_of_blocks(b))
            if texts:
                msgs.append({"role": "user", "content": "\n".join(texts)})
        elif role == "assistant":
            texts, calls = [], []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    texts.append(b.get("text", ""))
                elif t == "tool_use":
                    calls.append({
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {"name": b.get("name", ""),
                                     "arguments": json.dumps(b.get("input") or {})},
                    })
                # thinking / redacted_thinking blocks are dropped
            msg = {"role": "assistant", "content": "\n".join(texts)}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
        else:
            msgs.append({"role": role, "content": _text_of_blocks(content)})

    out = {"model": UPSTREAM_MODEL, "messages": msgs,
           "max_tokens": req.get("max_tokens", 8192)}
    if req.get("temperature") is not None:
        out["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        out["top_p"] = req["top_p"]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    tools = req.get("tools")
    if tools:
        out["tools"] = [{"type": "function", "function": {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
        }} for t in tools]
        tc = req.get("tool_choice") or {}
        ttype = tc.get("type")
        if ttype == "any":
            out["tool_choice"] = "required"
        elif ttype == "none":
            out["tool_choice"] = "none"
        elif ttype == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc.get("name", "")}}
        else:
            out["tool_choice"] = "auto"
    return out


# --------------------------------------------------------------------------
# OpenAI -> Anthropic
# --------------------------------------------------------------------------

def _finish_reason(openai_reason, has_tools):
    if has_tools:
        return "tool_use"
    return {"stop": "end_turn", "length": "max_tokens",
            "tool_calls": "tool_use", "function_call": "tool_use"}.get(
                openai_reason, "end_turn")


def openai_to_anthropic(data, req_model):
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    text = msg.get("content") or ""
    if text:
        content.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
        except (json.JSONDecodeError, TypeError):
            parsed = {"_raw_arguments": raw}
        content.append({"type": "tool_use", "id": call.get("id", ""),
                        "name": fn.get("name", ""), "input": parsed})
    usage = data.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message", "role": "assistant", "model": req_model,
        "content": content,
        "stop_reason": _finish_reason(choice.get("finish_reason"),
                                      bool(msg.get("tool_calls"))),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


# --------------------------------------------------------------------------
# EoCC compaction (operates on the converted OpenAI message list)
# --------------------------------------------------------------------------

def _upstream_chat(payload, timeout=1200):
    req = urllib.request.Request(
        UPSTREAM + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _is_plain_user(m):
    return m.get("role") == "user" and isinstance(m.get("content"), str)


def maybe_compact(messages, has_tools):
    """Return (messages, compacted?). Keeps system + first user (the task),
    replaces the middle with a model-written recap, keeps a recent window
    that starts at a plain user message so tool pairs are never split."""
    if THRESHOLD <= 0 or not has_tools:
        return messages, False
    if estimate_tokens(messages) <= THRESHOLD:
        return messages, False
    start = 1 if messages and messages[0].get("role") == "system" else 0
    if len(messages) <= start + 3:
        return messages, False
    # choose the cut: walk back from the end until RECENT_BUDGET is covered,
    # then advance to the next plain-user message boundary.
    acc, cut = 0, None
    for i in range(len(messages) - 1, start + 1, -1):
        acc += estimate_tokens(messages[i])
        if acc >= RECENT_BUDGET:
            cut = i
            break
    if cut is None:
        return messages, False
    while cut < len(messages) and not _is_plain_user(messages[cut]):
        cut += 1
    middle = messages[start + 1:cut]
    if not middle:
        return messages, False
    key = hashlib.sha256(json.dumps(middle, ensure_ascii=False).encode()).hexdigest()
    recap = _recap_cache.get(key)
    if recap is None:
        serial = json.dumps(middle, ensure_ascii=False)
        if len(serial) > 120000:
            serial = serial[:60000] + "\n...[truncated]...\n" + serial[-60000:]
        try:
            data = _upstream_chat({
                "model": UPSTREAM_MODEL, "max_tokens": 2048, "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "You write compact session recaps."},
                    {"role": "user", "content": RECAP_INSTRUCTION + serial},
                ]})
            recap = ((data["choices"][0]["message"].get("content") or "").strip())
        except Exception as e:  # never break the session over compaction
            debug_log({"compact_error": repr(e)})
            return messages, False
        if not recap:
            return messages, False
        _recap_cache.clear()
        _recap_cache[key] = recap
    head = messages[:start + 1]
    bridge = [
        {"role": "user",
         "content": "[Context was compacted to fit the local model's budget. "
                    "Recap of the earlier work:]\n\n" + recap},
        {"role": "assistant",
         "content": "Understood. I will continue from the recap."},
    ]
    return head + bridge + messages[cut:], True


# --------------------------------------------------------------------------
# Streaming: OpenAI SSE chunks -> Anthropic SSE events
# --------------------------------------------------------------------------

class StreamTranslator:
    def __init__(self, req_model):
        self.req_model = req_model
        self.msg_id = "msg_" + uuid.uuid4().hex[:24]
        self.started = False
        self.next_index = 0
        self.text_index = None
        self.tool_indices = {}   # openai tool_calls index -> anthropic block index
        self.tool_open = {}      # openai tool index -> bool (block started)
        self.finish_reason = None
        self.completion_tokens = 0
        self.prompt_tokens = 0

    def _emit(self, out, event, data):
        out.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")

    def _start(self, out):
        if self.started:
            return
        self.started = True
        self._emit(out, "message_start", {
            "type": "message_start",
            "message": {"id": self.msg_id, "type": "message", "role": "assistant",
                        "model": self.req_model, "content": [],
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def feed(self, chunk):
        """Consume one OpenAI chunk dict; return a list of SSE strings."""
        out = []
        self._start(out)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        usage = chunk.get("usage") or {}
        if usage.get("completion_tokens"):
            self.completion_tokens = usage["completion_tokens"]
        if usage.get("prompt_tokens"):
            self.prompt_tokens = usage["prompt_tokens"]
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]

        text = delta.get("content")
        if text:
            if self.text_index is None:
                self.text_index = self.next_index
                self.next_index += 1
                self._emit(out, "content_block_start", {
                    "type": "content_block_start", "index": self.text_index,
                    "content_block": {"type": "text", "text": ""}})
            self._emit(out, "content_block_delta", {
                "type": "content_block_delta", "index": self.text_index,
                "delta": {"type": "text_delta", "text": text}})

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            if idx not in self.tool_indices:
                self.tool_indices[idx] = self.next_index
                self.next_index += 1
                self.tool_open[idx] = False
            bidx = self.tool_indices[idx]
            if not self.tool_open[idx] and (tc.get("id") or fn.get("name")):
                self.tool_open[idx] = True
                self._emit(out, "content_block_start", {
                    "type": "content_block_start", "index": bidx,
                    "content_block": {"type": "tool_use",
                                      "id": tc.get("id", ""),
                                      "name": fn.get("name", ""),
                                      "input": {}}})
            frag = fn.get("arguments")
            if frag and self.tool_open[idx]:
                self._emit(out, "content_block_delta", {
                    "type": "content_block_delta", "index": bidx,
                    "delta": {"type": "input_json_delta", "partial_json": frag}})
        return out

    def close(self):
        out = []
        self._start(out)
        if self.text_index is not None:
            self._emit(out, "content_block_stop",
                       {"type": "content_block_stop", "index": self.text_index})
        for idx in sorted(self.tool_indices):
            if self.tool_open.get(idx):
                self._emit(out, "content_block_stop",
                           {"type": "content_block_stop",
                            "index": self.tool_indices[idx]})
        has_tools = bool(self.tool_indices)
        self._emit(out, "message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": _finish_reason(self.finish_reason, has_tools),
                      "stop_sequence": None},
            "usage": {"input_tokens": self.prompt_tokens,
                      "output_tokens": self.completion_tokens}})
        self._emit(out, "message_stop", {"type": "message_stop"})
        return out


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, code, message):
        self._send_json(code, {"type": "error",
                               "error": {"type": "api_error", "message": message}})

    def do_GET(self):
        if self.path in ("/", "/health"):
            return self._send_json(200, {"ok": True, "upstream": UPSTREAM,
                                         "model": UPSTREAM_MODEL})
        if self.path == "/stats":
            with _lock:
                return self._send_json(200, dict(STATS))
        return self._send_error(404, "not found")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send_error(400, "bad json")
        if self.path.startswith("/v1/messages/count_tokens"):
            return self._send_json(200, {"input_tokens": estimate_tokens(req)})
        if not self.path.startswith("/v1/messages"):
            return self._send_error(404, "not found")
        _stat(messages=1, last_message_ts=time.time(),
              probes=1 if self.headers.get("x-localcode-probe") else 0)
        _stat(inflight=1)
        try:
            self._messages(req)
        except Exception as e:
            debug_log({"handler_error": repr(e)})
            try:
                self._send_error(500, f"proxy error: {e}")
            except Exception:
                pass
        finally:
            _stat(inflight=-1)

    def _messages(self, req):
        req_model = req.get("model", UPSTREAM_MODEL)
        payload = anthropic_to_openai(req)
        payload["messages"], compacted = maybe_compact(
            payload["messages"], bool(payload.get("tools")))
        debug_log({"ts": time.time(), "model": req_model,
                   "msgs_in": len(req.get("messages", [])),
                   "msgs_out": len(payload["messages"]),
                   "compacted": compacted, "stream": bool(req.get("stream"))})
        if req.get("stream"):
            return self._stream(payload, req_model)
        payload["stream"] = False
        try:
            data = _upstream_chat(payload)
        except Exception as e:
            _stat(upstream_err=1, last_upstream_err=str(e)[:300])
            return self._send_error(502, f"upstream error: {e}")
        _stat(upstream_ok=1, last_upstream_ok_ts=time.time())
        return self._send_json(200, openai_to_anthropic(data, req_model))

    def _stream(self, payload, req_model):
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        request = urllib.request.Request(
            UPSTREAM + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(request, timeout=1800)
        except Exception as e:
            _stat(upstream_err=1, last_upstream_err=str(e)[:300])
            return self._send_error(502, f"upstream error: {e}")
        _stat(upstream_ok=1, last_upstream_ok_ts=time.time())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        tr = StreamTranslator(req_model)
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                _stat(last_chunk_ts=time.time())
                for sse in tr.feed(chunk):
                    self.wfile.write(sse.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                err = {"type": "error",
                       "error": {"type": "api_error", "message": str(e)}}
                self.wfile.write(f"event: error\ndata: {json.dumps(err)}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass
            return
        try:
            for sse in tr.close():
                self.wfile.write(sse.encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mightyCode for Muse proxy on http://127.0.0.1:{PORT} -> {UPSTREAM} ({UPSTREAM_MODEL})",
          file=sys.stderr)
    srv.serve_forever()
