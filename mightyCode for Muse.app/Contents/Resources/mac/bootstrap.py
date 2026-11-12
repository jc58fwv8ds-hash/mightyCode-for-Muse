"""LocalCode first-run bootstrap (Python stdlib only, system python3).

Serves the setup progress page on port 8472 (the backend owns 8471). Shows a progress page while
it: creates the data dir, installs a standalone `uv`, builds a venv (Python
3.12) with mlx-lm + huggingface_hub, downloads the pre-converted MLX model,
then starts the model server + API proxy + backend and redirects the
browser to the GUI.

If setup is already complete, it just (re)starts the services.
State lives in ~/Library/Application Support/LocalCode/.
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Keep Python (this process and the backend/proxy/model server it spawns, which
# inherit os.environ) from writing __pycache__ into the signed bundle.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

DATA = os.path.expanduser("~/Library/Application Support/LocalCode")
VENV = os.path.join(DATA, "venv")
UV = os.path.join(DATA, "bin", "uv")
MODEL_DIR = os.path.join(DATA, "models", "heretic-9b-mlx-4bit")
MODEL_REPO = "shi3z/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-MLX-4bit"
LOGS = os.path.join(DATA, "logs")
MARKER = os.path.join(DATA, "setup_complete.json")
PORT = 8471
SETUP_PORT = 8472
MODEL_PORT = 18080
PROXY_PORT = 18081

RESOURCES = os.path.dirname(os.path.abspath(__file__))  # .../Resources/mac
BACKEND = os.path.join(os.path.dirname(RESOURCES), "backend")

LOG_LINES = []
STATE = {"step": "starting", "done": False, "error": None}
LOCK = threading.Lock()


def log(msg):
    line = "[bootstrap] " + msg
    with LOCK:
        LOG_LINES.append(line)
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, "bootstrap.log"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd, **kw):
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    tail = (p.stdout or "")[-1500:] + (p.stderr or "")[-1500:]
    for ln in tail.splitlines()[-12:]:
        log("  " + ln)
    if p.returncode != 0:
        raise RuntimeError("command failed (%d): %s" % (p.returncode, " ".join(cmd)))
    return p


def set_step(s):
    with LOCK:
        STATE["step"] = s
    log("== " + s)


def setup():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    if os.path.exists(MARKER) and os.path.isdir(MODEL_DIR) and os.path.exists(os.path.join(VENV, "bin", "python")):
        return
    set_step("uv を用意しています")
    if not os.path.exists(UV):
        os.makedirs(os.path.join(DATA, "bin"), exist_ok=True)
        run(["/bin/sh", "-c",
             "curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=%s INSTALLER_NO_MODIFY_PATH=1 sh"
             % shlex.quote(os.path.join(DATA, "bin"))])
    set_step("Python環境（venv）を作成しています")
    if not os.path.exists(os.path.join(VENV, "bin", "python")):
        run([UV, "venv", "--python", "3.12", VENV])
    set_step("mlx-lm をインストールしています（数分かかります）")
    run([UV, "pip", "install", "--python", os.path.join(VENV, "bin", "python"),
         "mlx-lm", "huggingface_hub"])
    set_step("モデルをダウンロードしています（約4.7GB、回線により数分〜数十分）")
    if not os.path.isdir(MODEL_DIR) or not os.listdir(MODEL_DIR):
        code = ("from huggingface_hub import snapshot_download; "
                "snapshot_download(repo_id=%r, local_dir=%r)" % (MODEL_REPO, MODEL_DIR))
        run([os.path.join(VENV, "bin", "python"), "-c", code])
    with open(MARKER, "w") as f:
        json.dump({"model": MODEL_REPO, "ts": time.time()}, f)
    set_step("セットアップ完了")


def port_open(port):
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def start_services():
    set_step("モデルサーバーを起動しています")
    if not port_open(MODEL_PORT):
        out = open(os.path.join(LOGS, "model_server.log"), "ab")
        proc = subprocess.Popen(
            [os.path.join(VENV, "bin", "python"), "-m", "mlx_lm.server",
             "--model", MODEL_DIR, "--port", str(MODEL_PORT)],
            stdout=out, stderr=out, start_new_session=True)
        with open(os.path.join(DATA, "model_server.pid"), "w") as f:
            f.write(str(proc.pid))
    set_step("プロキシを起動しています")
    if not port_open(PROXY_PORT):
        penv = dict(os.environ,
                    UPSTREAM_BASE="http://127.0.0.1:%d/v1" % MODEL_PORT,
                    UPSTREAM_MODEL=MODEL_DIR, PROXY_PORT=str(PROXY_PORT))
        pout = open(os.path.join(LOGS, "proxy.log"), "ab")
        pproc = subprocess.Popen(
            [os.path.join(VENV, "bin", "python"),
             os.path.join(BACKEND, "proxy.py")],
            stdout=pout, stderr=pout, start_new_session=True, env=penv)
        with open(os.path.join(DATA, "proxy.pid"), "w") as f:
            f.write(str(pproc.pid))
        for _ in range(20):
            if port_open(PROXY_PORT):
                break
            time.sleep(0.5)
    set_step("バックエンドを起動しています")
    if port_open(PORT):
        log("backend already running on %d; not starting another" % PORT)
        with LOCK:
            STATE["done"] = True
        return
    env = dict(os.environ, MODEL_BASE="http://127.0.0.1:%d/v1" % MODEL_PORT,
               MODEL_NAME=MODEL_DIR, LOCALCODE_PORT=str(PORT),
               ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % PROXY_PORT,
               MODEL_PIDFILE=os.path.join(DATA, "model_server.pid"),
               PROXY_PIDFILE=os.path.join(DATA, "proxy.pid"))
    out = open(os.path.join(LOGS, "backend.log"), "ab")
    subprocess.Popen(
        [os.path.join(VENV, "bin", "python"), os.path.join(BACKEND, "server.py")],
        stdout=out, stderr=out, start_new_session=True, env=env)
    for _ in range(60):
        if port_open(PORT):
            break
        time.sleep(0.5)
    if not port_open(PORT):
        tail = ""
        try:
            with open(os.path.join(LOGS, "backend.log")) as f:
                tail = f.read()[-600:]
        except OSError:
            pass
        raise RuntimeError("backend did not start on port %d. backend.log tail: %s"
                           % (PORT, tail))
    with LOCK:
        STATE["done"] = True


def worker():
    try:
        setup()
        start_services()
    except Exception as e:
        with LOCK:
            STATE["error"] = str(e)
        log("ERROR: %r" % e)


PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>mightyCode for Muse セットアップ</title>
<style>body{font-family:"Hiragino Sans","Noto Sans CJK JP",sans-serif;background:#17181c;color:#e8e9eb;margin:40px}
pre{background:#121317;padding:14px;border-radius:8px;max-height:55vh;overflow:auto;font-size:12px;line-height:1.5}
.bar{color:#7fb2e5}</style></head><body>
<h2>mightyCode for Muse セットアップ</h2>
<p class="bar" id="step">準備中…</p>
<p>初回はモデルのダウンロード（約4.7GB）があるため時間がかかります。このページは自動で更新され、完了するとアプリに切り替わります。</p>
<pre id="log"></pre>
<script>
async function tick(){
  try{
    const r = await (await fetch('/state')).json();
    document.getElementById('step').textContent = r.error ? ('エラー: ' + r.error) : r.step;
    document.getElementById('log').textContent = r.log.join('\\n');
    if (r.done) { location.href = 'http://127.0.0.1:8471/'; return; }
  }catch(e){}
  setTimeout(tick, 1200);
}
tick();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/state":
            with LOCK:
                body = {"step": STATE["step"], "done": STATE["done"],
                        "error": STATE["error"], "log": LOG_LINES[-80:]}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    if port_open(SETUP_PORT):
        print("port busy; already running")
        sys.exit(0)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", SETUP_PORT), H).serve_forever()
