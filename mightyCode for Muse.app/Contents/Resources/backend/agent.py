"""Session management + Claude Code driver.

LocalCode's "body" is the real Claude Code CLI, driven over its
stream-json stdio protocol (the same transport the Agent SDK uses):

  claude -p --input-format stream-json --output-format stream-json
         --verbose --permission-mode default --permission-prompt-tool stdio

- After spawn we send an `initialize` control request; without it the CLI
  stalls after the first tool result (verified empirically).
- With --permission-prompt-tool stdio, every tool call that needs approval
  arrives as a `can_use_tool` control request. Guard (guard.py) is checked
  first: blocked calls are denied immediately with a Guard message and
  never reach the GUI. Other calls become GUI approval cards; the user's
  decision goes back as the control response.
- Claude Code talks to the local model through backend/proxy.py
  (ANTHROPIC_BASE_URL), so no Anthropic API is ever contacted.
"""
import collections
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import approvals
import guard
import modelwatch
import tools as tools_mod

PROXY_BASE = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:18081").rstrip("/")
CLI_READY_TIMEOUT = float(os.environ.get("LOCALCODE_CLI_READY_TIMEOUT", "45"))
MODEL_PROBE_TIMEOUT = float(os.environ.get("LOCALCODE_MODEL_PROBE_TIMEOUT", "180"))
# How long after a user message the CLI must have reached the proxy.
TURN_REACH_TIMEOUT = float(os.environ.get("LOCALCODE_TURN_REACH_TIMEOUT", "60"))
# How long we wait for the user to answer a macOS folder-access dialog.
FOLDER_ACCESS_TIMEOUT = float(os.environ.get("LOCALCODE_FOLDER_ACCESS_TIMEOUT", "300"))
APP_NAME = "mightyCode for Muse"

# Folders macOS guards with a per-app privacy (TCC) consent dialog. The first
# open() of such a folder BLOCKS until the user answers the dialog, and the
# consent is attributed to this app bundle, not to the `claude` binary.
_PROTECTED_DIRS = [
    ("~/Desktop", "デスクトップ"),
    ("~/Documents", "書類"),
    ("~/Downloads", "ダウンロード"),
    ("~/Library/Mobile Documents", "iCloud Drive"),
    ("~/Library/CloudStorage", "クラウドストレージ"),
    ("/Volumes", "外部／ネットワークボリューム"),
]


def protected_folder_label(path):
    """Japanese name of the macOS-protected area `path` lives in, or ""."""
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except OSError:
        return ""
    for base, label in _PROTECTED_DIRS:
        b = os.path.realpath(os.path.expanduser(base))
        if real == b or real.startswith(b + os.sep):
            return label
    return ""


def probe_folder_access(path):
    """List `path` on a worker thread. Returns (thread, result dict).

    On macOS this is what raises the "…フォルダへのアクセスを求めています"
    dialog; the listdir call does not return until the user has answered.
    result gets {"ok": True} or {"error": exc} once the call returns."""
    result = {}

    def work():
        try:
            os.listdir(path)
            result["ok"] = True
        except OSError as e:
            result["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, result


def proxy_stats():
    """Request counters kept by proxy.py (None if unreachable)."""
    try:
        with urllib.request.urlopen(PROXY_BASE + "/stats", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def probe_model(timeout=MODEL_PROBE_TIMEOUT):
    """Send one tiny Messages request through the proxy to the model server.

    Returns (ok, detail). A success means proxy -> mlx_lm.server POST
    /v1/chat/completions worked end to end."""
    body = json.dumps({"model": os.environ.get("MODEL_NAME", "local"),
                       "max_tokens": 1, "stream": False,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(
        PROXY_BASE + "/v1/messages", data=body,
        headers={"Content-Type": "application/json",
                 "x-localcode-probe": "1"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        if data.get("type") == "message":
            return True, f"{time.time() - t0:.1f}秒で応答"
        return False, f"想定外の応答: {json.dumps(data)[:200]}"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

MAX_RESULT = 30000

CLAUDE_CANDIDATES = [
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
]


def find_claude():
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    found = shutil.which("claude")
    if found:
        return found
    for cand in CLAUDE_CANDIDATES:
        p = os.path.expanduser(cand)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def claude_version(binary):
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=10)
        return (out.stdout or out.stderr).strip()
    except Exception:
        return ""


def _flatten(content):
    """Flatten Anthropic content (str | blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            else:
                parts.append(_flatten(b.get("content")))
    return "\n".join(p for p in parts if p)


class CCDriver:
    """Owns one `claude` subprocess and its stdio protocol."""

    def __init__(self, session):
        self.session = session
        self.proc = None
        self.write_lock = threading.Lock()
        self.cc_session_id = None
        self.ready = threading.Event()   # set when the CLI acks `initialize`
        self.init_id = None
        self.stderr_tail = collections.deque(maxlen=30)
        self.last_event_ts = 0.0
        self.generation = 0

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def spawn(self):
        binary = find_claude()
        if not binary:
            raise RuntimeError("claude CLI not found")
        args = [binary, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
                "--permission-mode", "default",
                "--permission-prompt-tool", "stdio"]
        if self.cc_session_id:
            args += ["--resume", self.cc_session_id]
        env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = os.environ.get(
            "ANTHROPIC_BASE_URL", "http://127.0.0.1:18081")
        env["ANTHROPIC_API_KEY"] = "localcode-local"
        env["ANTHROPIC_AUTH_TOKEN"] = "localcode-local"
        model = os.environ.get("MODEL_NAME", "")
        if model:
            env["ANTHROPIC_MODEL"] = model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = model
        self.ready.clear()
        self.stderr_tail.clear()
        self.generation += 1
        self.proc = subprocess.Popen(
            args, cwd=self.session.cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        proc, gen = self.proc, self.generation
        threading.Thread(target=self._reader, args=(proc, gen), daemon=True).start()
        threading.Thread(target=self._err_reader, args=(proc,), daemon=True).start()
        self.init_id = "init-" + uuid.uuid4().hex[:8]
        self.send({"type": "control_request",
                   "request_id": self.init_id,
                   "request": {"subtype": "initialize"}})

    def wait_ready(self, timeout):
        """True once the CLI answered `initialize`; False on timeout/exit."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ready.wait(0.25):
                return True
            if not self.alive():
                return False
        return self.ready.is_set()

    def kill(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def stderr_text(self):
        return "\n".join(self.stderr_tail)

    def send(self, obj):
        line = json.dumps(obj)
        with self.write_lock:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def send_user(self, text):
        self.send({"type": "user",
                   "message": {"role": "user", "content": text}})

    def respond_permission(self, request_id, behavior):
        self.send({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id,
            "response": behavior}})

    def interrupt(self):
        self.send({"type": "control_request",
                   "request_id": "int-" + uuid.uuid4().hex[:8],
                   "request": {"subtype": "interrupt"}})

    def _reader(self, proc, gen):
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.last_event_ts = time.time()
                if ev.get("type") == "control_response":
                    resp = ev.get("response") or {}
                    if resp.get("request_id") == self.init_id:
                        self.ready.set()
                try:
                    self.session.on_cc_event(ev, self)
                except Exception as e:
                    print(f"[driver] event handling failed: {e!r}",
                          file=sys.stderr)
        except Exception:
            pass
        try:
            code = proc.wait(timeout=5)
        except Exception:
            code = None
        if gen == self.generation:   # not a process we replaced on purpose
            self.session.on_driver_exit(code)

    def _err_reader(self, proc):
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    self.stderr_tail.append(line)
                    print(f"[claude] {line}", file=sys.stderr)
        except Exception:
            pass


class Session:
    """One GUI session: an event bus, an approval registry, one driver."""

    def __init__(self, sid, cwd, resume=None):
        self.id = sid
        self.cwd = cwd
        self.subscribers = []
        self.events_log = []
        self.emit_lock = threading.Lock()
        self.pending = {}          # call_id -> {"event": Event, "decision": None, "by": ..}
        self.pending_lock = threading.Lock()
        self.health = {"cli": "pending", "model": "pending", "cli_model": "pending"}
        self.health_detail = {}
        self.checking = False
        self.last_assistant_text = ""
        self.turn_id = 0
        self.resend_text = None
        self.auto_approve = False
        self.stop_flag = False
        self.running = False
        self.lock = threading.Lock()
        self.guard_log = []
        self.driver = CCDriver(self)
        if resume:
            self.driver.cc_session_id = resume
        self.tool_names = {}       # tool_use_id -> tool name
        self.started_at = time.time()
        self.started_by = "gui"    # "gui" (the person at the screen) | "muse"
        self.self_denied = set()   # tool_use_ids denied by Guard or the user

    # ---- events ------------------------------------------------------
    def emit(self, etype, data):
        with self.emit_lock:
            ev = {"type": etype, "ts": time.time(), "seq": len(self.events_log), **data}
            self.events_log.append(ev)
            for q in list(self.subscribers):
                q.put(ev)
        return ev

    # ---- health ------------------------------------------------------
    def set_health(self, stage, state, detail=""):
        self.health[stage] = state
        self.health_detail[stage] = detail
        self.emit("health", {"stage": stage, "state": state, "detail": detail,
                             "health": dict(self.health)})

    def start_check(self):
        """Run at session start (worker thread): CLI spawn + model reach."""
        if self.checking:
            return
        self.checking = True
        try:
            self._check_cli()
            self.set_health("model", "checking", "プロキシ経由でモデルサーバーに試験リクエストを送信中…")
            before = proxy_stats()
            ok, detail = probe_model()
            after = proxy_stats()
            if ok and before is not None and after is not None:
                detail += f"（プロキシ受信 {before.get('messages', 0)}→{after.get('messages', 0)}）"
            if ok:
                self.set_health("model", "ok", detail)
            else:
                self.set_health("model", "fail", "モデルサーバーで生成できませんでした: " + detail
                                + "（監視側で再確認し、必要ならモデルサーバーを再起動します）")
                if modelwatch.SUPERVISOR:
                    modelwatch.SUPERVISOR.kick()
            if self.health["cli_model"] != "ok":
                self.set_health("cli_model", "waiting",
                                "最初のタスク送信時に Claude Code → モデルの到達を確認します")
        finally:
            self.checking = False

    def _check_folder_access(self):
        """Make sure macOS lets this app read the work folder BEFORE the CLI
        is spawned. Without this the CLI blocks inside getcwd()/open() while
        the consent dialog is on screen, never answers `initialize`, and used
        to be killed after CLI_READY_TIMEOUT (shown as "exit -15")."""
        label = protected_folder_label(self.cwd)
        self.set_health("cli", "checking", "作業フォルダへのアクセスを確認しています…")
        thread, result = probe_folder_access(self.cwd)
        thread.join(2.0)
        if thread.is_alive():
            where = f"「{label}」フォルダ" if label else "作業フォルダ"
            self.set_health(
                "cli", "checking",
                f"macOS が {where}へのアクセス許可を求めています。画面に出ている"
                f"「“{APP_NAME}”から…アクセスを求められています」のダイアログで「許可」を"
                "押してください（押すまで待機します。ダイアログが見当たらない場合は"
                "他のウインドウの後ろを確認してください）")
            self.emit("notice", {"message":
                f"macOS のアクセス許可ダイアログへの回答を待っています（{where}）"})
            deadline = time.time() + FOLDER_ACCESS_TIMEOUT
            while thread.is_alive() and time.time() < deadline:
                thread.join(0.5)
        if thread.is_alive():
            self.set_health(
                "cli", "fail",
                f"{int(FOLDER_ACCESS_TIMEOUT)}秒待ちましたが、macOS のアクセス許可ダイアログに"
                "回答がありませんでした。ダイアログで「許可」を押してから「再チェック」を"
                "押してください")
            return False
        err = result.get("error")
        if err is not None:
            if isinstance(err, PermissionError):
                self.set_health(
                    "cli", "fail",
                    f"macOS がこのフォルダへのアクセスを許可していません: {self.cwd}\n"
                    f"「システム設定 → プライバシーとセキュリティ → ファイルとフォルダ」で "
                    f"“{APP_NAME}” の" + (f"「{label}」" if label else "該当フォルダ")
                    + "をオンにしてから「再チェック」を押すか、別の作業フォルダを指定してください")
            else:
                self.set_health("cli", "fail", f"作業フォルダを開けません: {err}")
            return False
        return True

    def _check_cli(self):
        if not self._check_folder_access():
            return False
        self.set_health("cli", "checking", "Claude Code CLI を起動しています…")
        try:
            if self.driver.alive():
                self.driver.generation += 1   # intentional restart: no exit notice
                self.driver.kill()
            self.driver.spawn()
        except Exception as e:
            self.set_health("cli", "fail", f"起動できませんでした: {e}")
            return False
        if self.driver.wait_ready(CLI_READY_TIMEOUT):
            self.set_health("cli", "ok", f"起動OK (pid {self.driver.proc.pid})")
            return True
        if self.driver.cc_session_id and not self.driver.alive():
            # --resume of a conversation that no longer exists: start fresh.
            old = self.driver.cc_session_id
            self.driver.cc_session_id = None
            self.emit("notice", {"message": f"以前の会話 ({old[:8]}…) を再開できなかったため、新しい会話で開始します"})
            try:
                self.driver.spawn()
            except Exception as e:
                self.set_health("cli", "fail", f"起動できませんでした: {e}")
                return False
            if self.driver.wait_ready(CLI_READY_TIMEOUT):
                self.set_health("cli", "ok", f"起動OK (pid {self.driver.proc.pid})")
                return True
        tail = self.driver.stderr_text()[-600:]
        state = "プロセスが終了しました" if not self.driver.alive() else f"{int(CLI_READY_TIMEOUT)}秒以内に応答がありません"
        hint = ""
        if self.driver.alive():
            hint = ("\nmacOS のアクセス許可ダイアログ（ファイルやフォルダへのアクセス）が"
                    "表示されたままになっていないか確認し、回答してから「再チェック」を押してください")
        self.set_health("cli", "fail", f"Claude Code CLI の初期化に失敗: {state}{hint}" + (f"\n{tail}" if tail else ""))
        self.driver.kill()
        return False

    def on_driver_exit(self, code):
        self.emit("notice", {"message": f"Claude Code プロセスが終了しました (exit {code})"})
        if self.health.get("cli") == "ok":
            self.set_health("cli", "fail", f"プロセスが終了しました (exit {code})。次の送信時に再起動します")
        with self.lock:
            was_running = self.running
            self.running = False
        if was_running:
            tail = self.driver.stderr_text()[-600:]
            self.emit("error", {"message": "Claude Code が応答途中で終了しました"
                                + (f"\n{tail}" if tail else "")})
            self.request_stop(interrupt=False)
            self.emit("done", {})

    def _turn_watchdog(self, turn_id, before, user_text, attempt):
        """Confirm the CLI actually sent this turn to the proxy AND the model."""
        start_ev = self.driver.last_event_ts
        deadline = time.time() + TURN_REACH_TIMEOUT
        proxy_seen = False
        b_msgs = (before or {}).get("messages", 0)
        b_ok = (before or {}).get("upstream_ok", 0)
        b_err = (before or {}).get("upstream_err", 0)
        while time.time() < deadline:
            time.sleep(1)
            if turn_id != self.turn_id or not self.running:
                return
            st = proxy_stats()
            if st is None or before is None:
                if self.driver.last_event_ts > start_ev:
                    return  # stats unavailable but the CLI is clearly working
                continue
            if not proxy_seen and st.get("messages", 0) > b_msgs:
                proxy_seen = True
                self.emit("turn_status", {"turn": turn_id, "stage": "proxy_reached",
                                          "message": "プロキシが受信しました。モデルに転送中…"})
            if st.get("upstream_ok", 0) > b_ok:
                self.emit("turn_status", {"turn": turn_id, "stage": "model_reached",
                                          "message": "モデルサーバーが生成を開始しました。応答を待っています…"})
                if self.health.get("cli_model") != "ok":
                    self.set_health("cli_model", "ok", "Claude Code → プロキシ → モデルの到達を確認")
                return
            if st.get("upstream_err", 0) > b_err:
                self.set_health("cli_model", "fail",
                                "プロキシからモデルサーバーへの転送が失敗: " + str(st.get("last_upstream_err", "")))
                if modelwatch.SUPERVISOR:
                    modelwatch.SUPERVISOR.kick()
                return  # Claude Code retries / reports the API error itself
        if turn_id != self.turn_id or not self.running:
            return
        if proxy_seen:
            # The CLI is fine; the model server is not answering.
            self.emit("turn_status", {"turn": turn_id, "stage": "model_slow",
                                      "message": f"プロキシは受信済み。{int(TURN_REACH_TIMEOUT)}秒経ってもモデルの応答開始がありません（長いプロンプトを処理中の可能性）。監視側で生成停止を検知した場合はモデルサーバーを再起動します"})
            if modelwatch.SUPERVISOR:
                modelwatch.SUPERVISOR.kick()
            return
        # Nothing reached the proxy: the CLI is wedged. Restart it and resend once.
        if attempt == 0:
            self.emit("notice", {"message": f"{int(TURN_REACH_TIMEOUT)}秒経ってもモデルにリクエストが届かないため、Claude Code を再起動して再送します"})
            self.set_health("cli_model", "checking", "再起動して再送中…")
            self.driver.generation += 1   # suppress on_driver_exit for this kill
            self.driver.kill()
            if not self._check_cli():
                self._fail_turn("Claude Code を再起動できませんでした")
                return
            before = proxy_stats()
            self.driver.send_user(user_text)
            threading.Thread(target=self._turn_watchdog,
                             args=(turn_id, before, user_text, 1), daemon=True).start()
            return
        self.set_health("cli_model", "fail", "Claude Code からモデルにリクエストが届きません")
        if modelwatch.SUPERVISOR:
            modelwatch.SUPERVISOR.kick()
        self._fail_turn("タスクを送信しましたが、モデルサーバーにリクエストが届きませんでした。"
                        "モデルサーバー(18080)/プロキシ(18081)の状態を確認してください")

    def _fail_turn(self, message):
        self.emit("error", {"message": message})
        self.request_stop()
        with self.lock:
            self.running = False
        self.emit("done", {})

    def subscribe(self):
        q = queue.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)

    # ---- approvals ---------------------------------------------------
    def resolve_approval(self, call_id, decision, by="gui", note=""):
        """First answer wins (GUI card, Muse decision file, or Stop)."""
        with self.pending_lock:
            p = self.pending.get(call_id)
            if not p or p["event"].is_set():
                return False
            p["decision"] = bool(decision)
            p["by"] = by
            p["note"] = note
            p["event"].set()
            return True

    def _wait_approval(self, call_id):
        """Returns (decision: bool, by: str, note: str)."""
        p = self.pending[call_id]
        while not p["event"].wait(0.5):
            if self.stop_flag:
                self.resolve_approval(call_id, False, "stop", "停止されました")
        with self.pending_lock:
            self.pending.pop(call_id, None)
        return bool(p["decision"]), p.get("by") or "gui", p.get("note") or ""

    def request_approval(self, call_id, tool, args, diff, cc_reason=""):
        """Ask the GUI and the Muse file bridge at the same time; block."""
        with self.pending_lock:
            self.pending[call_id] = {"event": threading.Event(), "decision": None}
        bridge = approvals.BRIDGE
        bridge_file = None
        if bridge is not None:
            info = approvals.describe(tool, args, self.cwd,
                                      reason=self.last_assistant_text[-1500:],
                                      cc_reason=cc_reason)
            record = {"id": call_id, "session_id": self.id, "cwd": self.cwd,
                      "tool": tool, **info, "args": args,
                      "diff": (diff or "")[:6000] or None}
            try:
                bridge_file = bridge.publish(
                    record, lambda d, by, note: self.resolve_approval(call_id, d, by, note))
            except Exception as e:
                self.emit("notice", {"message": f"承認ブリッジへの書き出しに失敗: {e}"})
        self.emit("approval_required", {"call_id": call_id, "tool": tool,
                                        "args": args, "diff": diff,
                                        "bridge_file": bridge_file})
        decision, by, note = self._wait_approval(call_id)
        if bridge is not None and bridge_file:
            bridge.finalize(call_id, decision, by, note)
        self.emit("approval_resolved", {"call_id": call_id, "decision": decision,
                                        "by": by, "note": note})
        return decision

    # ---- lifecycle ---------------------------------------------------
    def request_stop(self, interrupt=True):
        self.stop_flag = True
        for cid in list(self.pending):
            self.resolve_approval(cid, False, "stop", "停止されました")
        if interrupt and self.driver.alive():
            self.driver.interrupt()

    def run_turn(self, user_text, by="gui"):
        """Called from a worker thread by server.py. `by` says who sent the
        message: "gui" (the person at the screen) or "muse" (HTTP API)."""
        with self.lock:
            if self.running:
                self.emit("error", {"message": "already running"})
                return
            self.running = True
            self.stop_flag = False
        self.turn_id += 1
        turn_id = self.turn_id
        self.emit("user_message", {"content": user_text, "by": by})
        # Wait for an in-flight start check so we never race two spawns.
        # (the check may be waiting on a macOS folder-access dialog)
        for _ in range(int((FOLDER_ACCESS_TIMEOUT + 2 * CLI_READY_TIMEOUT + 10) * 4)):
            if not self.checking:
                break
            time.sleep(0.25)
        if not self.driver.alive() or not self.driver.ready.is_set():
            self.emit("turn_status", {"turn": turn_id, "stage": "starting_cli",
                                      "message": "Claude Code を起動しています…"})
            if not self._check_cli():
                self.emit("error", {"message": "Claude Codeを起動できませんでした: "
                                    + self.health_detail.get("cli", "")})
                self.emit("done", {})
                with self.lock:
                    self.running = False
                return
        before = proxy_stats()
        self.driver.send_user(user_text)
        self.emit("turn_status", {"turn": turn_id, "stage": "sent",
                                  "message": "Claude Code に送信しました。モデルへの到達を確認中…"})
        threading.Thread(target=self._turn_watchdog,
                         args=(turn_id, before, user_text, 0), daemon=True).start()

    # ---- Claude Code event handling ----------------------------------
    def on_cc_event(self, ev, driver):
        t = ev.get("type")
        if t == "system":
            self._on_system(ev)
        elif t == "assistant":
            self._on_assistant(ev)
        elif t == "user":
            self._on_user(ev)
        elif t == "control_request":
            threading.Thread(target=self._on_control_request, args=(ev, driver),
                             daemon=True).start()
        elif t == "result":
            self._on_result(ev)
        # control_response (init ack) and unknown types need no handling

    def _on_system(self, ev):
        subtype = ev.get("subtype")
        if subtype == "init":
            self.driver.cc_session_id = ev.get("session_id") or self.driver.cc_session_id
            self.emit("cc_session", {"cc_session_id": self.driver.cc_session_id})
        elif subtype == "permission_denied":
            tool_use_id = ev.get("tool_use_id")
            if tool_use_id in self.self_denied:
                return  # our own Guard/user denial; already surfaced
            reason = ev.get("message") or ev.get("decision_reason") or ""
            self.emit("cc_denied", {"tool": ev.get("tool_name", ""),
                                    "reason": reason})
        elif subtype == "compact_boundary":
            meta = ev.get("compact_metadata") or {}
            self.emit("compacted",
                      {"tokens": meta.get("post_tokens") or 0})

    def _on_assistant(self, ev):
        msg = ev.get("message") or {}
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text = (b.get("text") or "").strip()
                if text:
                    self.last_assistant_text = text
                    self.emit("assistant_text", {"content": text})
            elif b.get("type") == "tool_use":
                call_id = b.get("id") or ""
                name = b.get("name", "")
                args = b.get("input") or {}
                self.tool_names[call_id] = name
                diff = ""
                if name in ("Write", "Edit"):
                    try:
                        diff = tools_mod.preview_diff(self.cwd, args)
                    except Exception:
                        diff = ""
                self.emit("tool_call", {"call_id": call_id, "tool": name,
                                        "args": args, "diff": diff})

    def _on_user(self, ev):
        msg = ev.get("message") or {}
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                call_id = b.get("tool_use_id", "")
                text = _flatten(b.get("content"))
                if len(text) > MAX_RESULT:
                    text = text[:MAX_RESULT] + "\n...[truncated]"
                self.emit("tool_result", {
                    "call_id": call_id,
                    "tool": self.tool_names.get(call_id, ""),
                    "result": text})

    def _on_control_request(self, ev, driver):
        req = ev.get("request") or {}
        request_id = ev.get("request_id", "")
        if req.get("subtype") != "can_use_tool":
            driver.send({"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id,
                "error": "unsupported control request"}})
            return
        tool = req.get("tool_name", "")
        args = req.get("input") or {}
        call_id = req.get("tool_use_id") or request_id

        decision, reason = guard.check(tool, args)
        if decision == "block":
            self.guard_log.append({"tool": tool, "args": args,
                                   "reason": reason, "ts": time.time()})
            self.self_denied.add(call_id)
            self.emit("guard_blocked", {"call_id": call_id, "tool": tool,
                                        "args": args, "reason": reason})
            driver.respond_permission(request_id, {
                "behavior": "deny",
                "message": f"Blocked by Guard: {reason}. Do not retry this "
                           "exact call; choose a different approach or ask "
                           "the user."})
            return

        if not self.auto_approve:
            diff = ""
            if tool in ("Write", "Edit"):
                try:
                    diff = tools_mod.preview_diff(self.cwd, args)
                except Exception:
                    diff = ""
            cc_reason = str(req.get("decision_reason") or req.get("reason") or "")
            if req.get("blocked_path"):
                cc_reason = (cc_reason + " " if cc_reason else "") + \
                    f"（作業フォルダの許可範囲外のパス: {req['blocked_path']}）"
            if not self.request_approval(call_id, tool, args, diff, cc_reason):
                self.self_denied.add(call_id)
                driver.respond_permission(request_id, {
                    "behavior": "deny",
                    "message": "The user denied this tool call. Do not "
                               "retry it as-is; ask or adjust."})
                return
        driver.respond_permission(request_id, {
            "behavior": "allow", "updatedInput": args})

    def _on_result(self, ev):
        usage = ev.get("usage") or {}
        if usage:
            est = (usage.get("input_tokens", 0)
                   + usage.get("cache_read_input_tokens", 0)
                   + usage.get("cache_creation_input_tokens", 0)
                   + usage.get("output_tokens", 0))
            self.emit("usage", {"usage": usage, "est_tokens": est})
        if ev.get("is_error") or (ev.get("subtype") or "").startswith("error"):
            self.emit("error", {"message": str(
                ev.get("error") or ev.get("result") or ev.get("subtype"))})
        if self.health.get("cli_model") != "ok":
            self.set_health("cli_model", "ok", "Claude Code → モデルの応答を確認")
        self.emit("done", {})
        with self.lock:
            self.running = False
