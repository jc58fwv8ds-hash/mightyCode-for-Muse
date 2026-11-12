"""Model-server / proxy supervisor.

`GET /v1/models == 200` is NOT treated as healthy: mlx_lm.server can lose its
generation thread and keep answering HTTP. Health here means a real
generation request (max_tokens=1, POST /v1/chat/completions) completes.

Loop (every CHECK_INTERVAL seconds):
- If a generation is in flight (proxy /stats inflight > 0) we do not probe
  (it would queue behind the real work). Instead we watch stream progress:
  no chunk for STALL_TIMEOUT seconds while a request is in flight => hung.
- If a real generation succeeded within FRESH_OK seconds => healthy.
- Otherwise send a max_tokens=1 probe directly to the model server.
- FAIL_LIMIT consecutive failures (or a hang) => restart mlx_lm.server.
- Proxy: /health fails FAIL_LIMIT times => restart proxy.py.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

E = os.environ.get
DATA = os.path.expanduser("~/Library/Application Support/LocalCode")
LOGS = os.path.join(DATA, "logs")
MODEL_BASE = E("MODEL_BASE", "http://127.0.0.1:18080/v1").rstrip("/")
MODEL_NAME = E("MODEL_NAME", os.path.join(DATA, "models", "heretic-9b-mlx-4bit"))
PROXY_BASE = E("ANTHROPIC_BASE_URL", "http://127.0.0.1:18081").rstrip("/")
MODEL_PIDFILE = E("MODEL_PIDFILE", os.path.join(DATA, "model_server.pid"))
PROXY_PIDFILE = E("PROXY_PIDFILE", os.path.join(DATA, "proxy.pid"))
VENV_PY = os.path.join(DATA, "venv", "bin", "python")
BACKEND = os.path.dirname(os.path.abspath(__file__))

CHECK_INTERVAL = float(E("LOCALCODE_HEALTH_INTERVAL", "45"))
PROBE_TIMEOUT = float(E("LOCALCODE_PROBE_TIMEOUT", "90"))
FRESH_OK = float(E("LOCALCODE_FRESH_OK", "60"))
STALL_TIMEOUT = float(E("LOCALCODE_STALL_TIMEOUT", "600"))
FAIL_LIMIT = int(E("LOCALCODE_FAIL_LIMIT", "2"))
BOOT_WAIT = float(E("LOCALCODE_MODEL_BOOT_WAIT", "240"))
AUTO_RESTART = E("LOCALCODE_AUTO_RESTART", "1") != "0"


def _port(base):
    try:
        return int(base.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
    except Exception:
        return None


def _get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def http_ok(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def generation_probe(timeout=PROBE_TIMEOUT):
    """(ok, detail, seconds). Direct to mlx_lm.server, bypassing the proxy."""
    body = json.dumps({"model": MODEL_NAME, "max_tokens": 1, "stream": False,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(MODEL_BASE + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        dt = time.time() - t0
        if data.get("choices"):
            return True, f"生成OK ({dt:.1f}s)", dt
        return False, "choices のない応答: " + json.dumps(data)[:200], dt
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", time.time() - t0


def _pids_on_port(port):
    try:
        out = subprocess.run(["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{port}",
                              "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5)
        return [int(x) for x in out.stdout.split()]
    except Exception:
        return []


def _kill(pidfile, port):
    pids = set(_pids_on_port(port) if port else [])
    try:
        with open(pidfile) as f:
            pids.add(int(f.read().strip()))
    except Exception:
        pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    deadline = time.time() + 10
    while time.time() < deadline and port and _pids_on_port(port):
        time.sleep(0.5)
    for pid in _pids_on_port(port) if port else []:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return sorted(pids)


class Supervisor:
    def __init__(self, active_cb=lambda: False):
        self.active_cb = active_cb      # True while any GUI session is mid-turn
        self.lock = threading.Lock()
        self.restart_lock = threading.Lock()
        self.wake = threading.Event()
        self.state = {
            "model": {"state": "unknown", "detail": "未確認", "last_ok_ts": 0.0,
                      "last_check_ts": 0.0, "fails": 0, "restarts": 0,
                      "http_ok": None},
            "proxy": {"state": "unknown", "detail": "未確認", "fails": 0, "restarts": 0},
            "events": [],
        }

    # -- public -------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="model-supervisor").start()

    def kick(self):
        """Check now (e.g. a session start probe just failed)."""
        self.wake.set()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def model_ok(self):
        return self.state["model"]["state"] == "ok"

    # -- internals ----------------------------------------------------
    def _log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        print("[supervisor] " + msg, flush=True)
        with self.lock:
            self.state["events"] = (self.state["events"] + [line])[-30:]

    def _set(self, which, **kw):
        with self.lock:
            self.state[which].update(kw)

    def _loop(self):
        time.sleep(3)
        while True:
            try:
                self.check_once()
            except Exception as e:
                self._log(f"check error: {e!r}")
            self.wake.wait(CHECK_INTERVAL)
            self.wake.clear()

    def check_once(self):
        self._check_proxy()
        self._check_model()

    def _check_proxy(self):
        p = self.state["proxy"]
        if http_ok(PROXY_BASE + "/health"):
            self._set("proxy", state="ok", detail="応答あり", fails=0)
            return
        fails = p["fails"] + 1
        self._set("proxy", state="fail", detail="/health 応答なし", fails=fails)
        if fails >= FAIL_LIMIT and AUTO_RESTART:
            self.restart_proxy("/health が連続で応答しません")

    def _check_model(self):
        now = time.time()
        m = self.state["model"]
        http = http_ok(MODEL_BASE + "/models")
        self._set("model", http_ok=http, last_check_ts=now)
        stats = None
        try:
            stats = _get_json(PROXY_BASE + "/stats")
        except Exception:
            pass

        if stats and stats.get("inflight", 0) > 0:
            last_progress = max(stats.get("last_chunk_ts") or 0, stats.get("last_message_ts") or 0)
            idle = now - last_progress
            if idle > STALL_TIMEOUT:
                self._fail(f"生成中のリクエストが {int(idle)} 秒進んでいません（生成スレッド停止の疑い）",
                           force=True)
            else:
                self._set("model", state="ok" if m["state"] != "restarting" else m["state"],
                          detail=f"生成中（最終進捗 {int(idle)} 秒前）")
            return
        if self.active_cb() and stats and now - (stats.get("last_message_ts") or 0) < 30:
            return   # a turn just started; let the turn watchdog handle it

        if not http:
            self._fail("/v1/models に応答しません（プロセス停止の疑い）")
            return
        # A real generation that finished recently counts as a passed probe.
        if stats and now - (stats.get("last_upstream_ok_ts") or 0) < FRESH_OK \
                and (stats.get("last_chunk_ts") or 0) >= (stats.get("last_message_ts") or 0):
            self._set("model", state="ok", detail="直近の生成が成功しています", fails=0,
                      last_ok_ts=now)
            return
        ok, detail, _ = generation_probe()
        if ok:
            self._set("model", state="ok", detail=detail, fails=0, last_ok_ts=time.time())
        else:
            self._fail("/v1/models は 200 だが生成リクエストが失敗: " + detail)

    def _fail(self, detail, force=False):
        fails = self.state["model"]["fails"] + 1
        self._set("model", state="fail", detail=detail, fails=fails)
        self._log(f"model check failed ({fails}/{FAIL_LIMIT}): {detail}")
        if (force or fails >= FAIL_LIMIT) and AUTO_RESTART:
            self.restart_model(detail)
        elif fails < FAIL_LIMIT:
            # re-check soon instead of waiting a full interval
            threading.Timer(10, self.kick).start()

    # -- restarts -----------------------------------------------------
    def restart_model(self, why):
        if not self.restart_lock.acquire(blocking=False):
            return
        try:
            self._set("model", state="restarting", detail=f"再起動中: {why}")
            self._log(f"restarting model server: {why}")
            killed = _kill(MODEL_PIDFILE, _port(MODEL_BASE))
            self._log(f"stopped pids {killed}")
            out = open(os.path.join(LOGS, "model_server.log"), "ab")
            proc = subprocess.Popen(
                [VENV_PY, "-m", "mlx_lm.server", "--model", MODEL_NAME,
                 "--port", str(_port(MODEL_BASE) or 18080)],
                stdout=out, stderr=out, start_new_session=True)
            with open(MODEL_PIDFILE, "w") as f:
                f.write(str(proc.pid))
            deadline = time.time() + BOOT_WAIT
            while time.time() < deadline and not http_ok(MODEL_BASE + "/models"):
                if proc.poll() is not None:
                    break
                time.sleep(2)
            ok, detail, _ = generation_probe()
            restarts = self.state["model"]["restarts"] + 1
            if ok:
                self._set("model", state="ok", detail=f"再起動後 {detail}", fails=0,
                          last_ok_ts=time.time(), restarts=restarts)
                self._log(f"model server restarted (pid {proc.pid}); {detail}")
            else:
                self._set("model", state="fail", detail=f"再起動後も生成できません: {detail}",
                          restarts=restarts, fails=0)
                self._log(f"model server restart did not recover: {detail}")
        finally:
            self.restart_lock.release()

    def restart_proxy(self, why):
        self._log(f"restarting proxy: {why}")
        _kill(PROXY_PIDFILE, _port(PROXY_BASE))
        env = dict(os.environ, UPSTREAM_BASE=MODEL_BASE, UPSTREAM_MODEL=MODEL_NAME,
                   PROXY_PORT=str(_port(PROXY_BASE) or 18081))
        out = open(os.path.join(LOGS, "proxy.log"), "ab")
        proc = subprocess.Popen([VENV_PY if os.path.exists(VENV_PY) else sys.executable,
                                 os.path.join(BACKEND, "proxy.py")],
                                stdout=out, stderr=out, start_new_session=True, env=env)
        with open(PROXY_PIDFILE, "w") as f:
            f.write(str(proc.pid))
        for _ in range(20):
            if http_ok(PROXY_BASE + "/health"):
                break
            time.sleep(0.5)
        ok = http_ok(PROXY_BASE + "/health")
        self._set("proxy", state="ok" if ok else "fail", fails=0,
                  restarts=self.state["proxy"]["restarts"] + 1,
                  detail="再起動しました" if ok else "再起動後も応答しません")


SUPERVISOR = None


def init(active_cb):
    global SUPERVISOR
    if SUPERVISOR is None:
        SUPERVISOR = Supervisor(active_cb)
        SUPERVISOR.start()
    return SUPERVISOR
