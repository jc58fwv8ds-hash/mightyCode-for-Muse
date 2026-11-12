"""LocalCode backend server (Python stdlib only).

Serves the GUI and bridges it to the real Claude Code CLI (driven by
backend/agent.py) with SSE events and GUI approvals. Binds to 127.0.0.1.

Session stability:
- Every backend process has a BOOT_ID (in /api/status and the SSE hello
  event). The GUI compares it to detect a backend restart and then
  reloads / re-creates its session instead of talking to a dead one.
- Unknown sessions answer 410 {"error": "session_lost"} so the GUI can
  recover instead of silently spinning.
- SSE events carry `id:` = seq; reconnects resume from Last-Event-ID.
- modelwatch.py supervises the model server and the proxy: health means a
  real generation succeeds (not just GET /v1/models == 200), and hung or
  dead servers are restarted.

Approvals are also mirrored to ~/Library/Application Support/LocalCode/
approvals/ (see approvals.py) so Muse can answer them.
"""
import json
import os
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import agent as agent_mod
import approvals
import modelwatch

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(os.path.dirname(HERE), "frontend")
PORT = int(os.environ.get("LOCALCODE_PORT", "8471"))
MODEL_BASE = os.environ.get("MODEL_BASE", "http://127.0.0.1:18080/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "heretic-9b-mlx-4bit")
PROXY_BASE = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:18081")

BOOT_ID = uuid.uuid4().hex[:12]
STARTED_AT = time.time()

SESSIONS = {}
LOCK = threading.Lock()
TESTS = {}   # approval-bridge self tests: id -> {"event", "result"}

_CLAUDE_BIN = agent_mod.find_claude()
_CLAUDE_VERSION = agent_mod.claude_version(_CLAUDE_BIN) if _CLAUDE_BIN else ""

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}


def _operator(value):
    """Who is driving: "muse" (through the HTTP API) or "gui" (the person)."""
    return "muse" if str(value or "").strip().lower() == "muse" else "gui"


def _session_summary(s):
    return {"id": s.id, "cwd": s.cwd, "running": s.running,
            "health": dict(s.health), "cli_alive": s.driver.alive(),
            "started_at": s.started_at, "started_by": s.started_by,
            "events": len(s.events_log),
            "pending_approvals": len(s.pending),
            "last_event_at": s.events_log[-1]["ts"] if s.events_log else s.started_at}


def _any_running():
    return any(s.running for s in list(SESSIONS.values()))


def _kill_pidfile(env_key):
    pidfile = os.environ.get(env_key, "")
    try:
        if pidfile and os.path.exists(pidfile):
            import signal
            with open(pidfile) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Safari must never serve a stale GUI or stale API answers from cache.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-LocalCode-Boot", BOOT_ID)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _lost(self):
        return self._send(410, {"error": "session_lost", "boot_id": BOOT_ID,
                                "message": "このセッションはバックエンド側に存在しません"
                                           "（バックエンドが再起動した可能性があります）"})

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/api/events":
            return self._events(qs)
        if u.path == "/api/status":
            sid = qs.get("session", [""])[0]
            sess = SESSIONS.get(sid) if sid else None
            sup = modelwatch.SUPERVISOR.snapshot() if modelwatch.SUPERVISOR else {}
            m = sup.get("model", {})
            body = {
                "boot_id": BOOT_ID, "started_at": STARTED_AT,
                "model_base": MODEL_BASE, "model": MODEL_NAME,
                # "reachable" now means a real generation succeeded recently,
                # not merely GET /v1/models == 200.
                "model_reachable": m.get("state") == "ok",
                "model_state": m.get("state"), "model_detail": m.get("detail"),
                "model_http_ok": m.get("http_ok"),
                "model_restarts": m.get("restarts", 0),
                "proxy_base": PROXY_BASE,
                "proxy_state": sup.get("proxy", {}).get("state"),
                "supervisor_events": sup.get("events", [])[-5:],
                "claude_found": bool(_CLAUDE_BIN),
                "claude_version": _CLAUDE_VERSION,
                "approvals_dir": approvals.DIR,
                "approvals_pending": len(approvals.BRIDGE.pending_ids()) if approvals.BRIDGE else 0,
            }
            if sid:
                body["session_alive"] = sess is not None
                if sess:
                    body["session"] = {"running": sess.running, "health": sess.health,
                                       "cli_alive": sess.driver.alive()}
            return self._send(200, body)
        if u.path == "/api/sessions":
            # Every live session, so the GUI can join one Muse started and
            # Muse can find the one the person opened.
            items = sorted((_session_summary(s) for s in list(SESSIONS.values())),
                           key=lambda x: x["started_at"], reverse=True)
            return self._send(200, {"boot_id": BOOT_ID, "sessions": items})
        if u.path == "/api/approval_test":
            t = TESTS.get(qs.get("id", [""])[0])
            if not t:
                return self._send(404, {"error": "no such test"})
            return self._send(200, {"id": qs["id"][0], "result": t["result"]})
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        path = os.path.normpath(os.path.join(FRONTEND, rel))
        if path.startswith(FRONTEND) and os.path.isfile(path):
            ext = os.path.splitext(path)[1]
            with open(path, "rb") as f:
                return self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))
        return self._send(404, {"error": "not found"})

    def _events(self, qs):
        sid = qs.get("session", [""])[0]
        sess = SESSIONS.get(sid)
        if not sess:
            return self._lost()
        last = self.headers.get("Last-Event-ID") or qs.get("since", [""])[0]
        try:
            start = int(last) + 1 if last not in ("", None) else 0
        except ValueError:
            start = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-LocalCode-Boot", BOOT_ID)
        self.end_headers()
        q = sess.subscribe()
        try:
            hello = {"type": "hello", "boot_id": BOOT_ID, "session": sid,
                     "health": sess.health, "running": sess.running}
            self.wfile.write(f"retry: 2000\nevent: hello\ndata: {json.dumps(hello)}\n\n".encode())
            sent = start - 1
            for ev in list(sess.events_log[start:]):
                self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n".encode())
                sent = ev["seq"]
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=10)
                    if ev["seq"] <= sent:
                        continue
                    self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n".encode())
                    sent = ev["seq"]
                except Exception:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sess.unsubscribe(q)

    def do_POST(self):
        try:
            body = self._body()
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/session":
            if not _CLAUDE_BIN:
                return self._send(400, {"error":
                    "Claude Code (claude コマンド) が見つかりません。"
                    "インストールしてから再度お試しください。"})
            cwd = os.path.expanduser(body.get("cwd") or os.getcwd())
            if not os.path.isdir(cwd):
                return self._send(400, {"error": f"not a directory: {cwd}"})
            sid = uuid.uuid4().hex[:12]
            resume = body.get("resume") or None
            sess = agent_mod.Session(sid, cwd, resume=resume)
            sess.started_by = _operator(body.get("by"))
            with LOCK:
                SESSIONS[sid] = sess
            threading.Thread(target=sess.start_check, daemon=True).start()
            return self._send(200, {"session_id": sid, "cwd": cwd, "boot_id": BOOT_ID,
                                    "resume": resume, "by": sess.started_by})
        if self.path == "/api/quit":
            _kill_pidfile("MODEL_PIDFILE")
            _kill_pidfile("PROXY_PIDFILE")
            for s in list(SESSIONS.values()):
                s.driver.kill()
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (threading.Event().wait(0.4), os._exit(0)),
                             daemon=True).start()
            return
        if self.path == "/api/approval_test":
            return self._approval_test(body)
        if self.path == "/api/health/check":
            if modelwatch.SUPERVISOR:
                modelwatch.SUPERVISOR.kick()
            return self._send(200, {"ok": True})
        sess = SESSIONS.get(body.get("session", ""))
        if not sess:
            return self._lost()
        if self.path == "/api/chat":
            msg = body.get("message", "")
            if sess.running:
                return self._send(409, {"error": "busy", "message": "前のタスクを実行中です"})
            threading.Thread(target=sess.run_turn,
                             args=(msg, _operator(body.get("by"))), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/api/check":
            threading.Thread(target=sess.start_check, daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/api/approve":
            ok = sess.resolve_approval(body.get("call_id", ""),
                                       bool(body.get("decision")),
                                       _operator(body.get("by")),
                                       str(body.get("note") or ""))
            return self._send(200, {"ok": ok})
        if self.path == "/api/stop":
            sess.request_stop()
            return self._send(200, {"ok": True})
        if self.path == "/api/auto":
            sess.auto_approve = bool(body.get("auto_approve"))
            return self._send(200, {"ok": True, "auto_approve": sess.auto_approve})
        return self._send(404, {"error": "not found"})

    def _approval_test(self, body):
        """Publish a harmless synthetic approval request to verify the bridge.

        Nothing is executed whatever the answer; the result is only recorded.
        """
        bridge = approvals.BRIDGE
        tid = "test-" + uuid.uuid4().hex[:10]
        tool = "Bash"
        args = {"command": body.get("command") or
                "curl -L -o /tmp/localcode-bridge-test.md https://huggingface.co/README.md",
                "description": "承認ブリッジ往復テスト（実行はされません）"}
        info = approvals.describe(tool, args, os.path.expanduser("~"),
                                  reason="承認ブリッジの動作確認用のテストリクエストです。")
        t = {"event": threading.Event(), "result": None, "lock": threading.Lock()}
        TESTS[tid] = t

        def resolver(decision, by, note):
            with t["lock"]:
                if t["event"].is_set():
                    return False
                t["result"] = {"decision": decision, "by": by, "note": note}
                t["event"].set()
                return True

        path = bridge.publish({"id": tid, "session_id": "approval-test", "test": True,
                               "cwd": os.path.expanduser("~"), "tool": tool, **info,
                               "args": args}, resolver)

        def waiter():
            if not t["event"].wait(float(body.get("timeout") or 300)):
                resolver(None, "timeout", "")
            r = t["result"]
            bridge.finalize(tid, r["decision"], r["by"], r["note"])
        threading.Thread(target=waiter, daemon=True).start()
        return self._send(200, {"id": tid, "request_file": path,
                                "decision_file": bridge.decision_path(tid)})


if __name__ == "__main__":
    approvals.init(BOOT_ID)
    modelwatch.init(_any_running)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"mightyCode for Muse backend on http://127.0.0.1:{PORT} boot={BOOT_ID} "
          f"(claude: {_CLAUDE_VERSION or 'NOT FOUND'}, model: {MODEL_BASE}, "
          f"approvals: {approvals.DIR})", flush=True)
    srv.serve_forever()
