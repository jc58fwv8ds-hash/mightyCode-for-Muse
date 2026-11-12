"""File-based approval bridge (LocalCode <-> Muse).

Every approval the GUI asks for is also written to

    ~/Library/Application Support/LocalCode/approvals/<id>.request.json

with everything needed to present it to the user in another chat
(tool, command, URLs/hosts, paths, reason, risk notes, a Japanese summary).
Muse answers by writing

    ~/Library/Application Support/LocalCode/approvals/<id>.decision.json
    {"decision": "approve" | "deny", "by": "Muse", "note": "..."}

The first answer wins: the GUI card (POST /api/approve) and the decision
file race, and whichever arrives first resolves the tool call. When a
request is resolved (by either side, by Stop, or because the backend
restarted) the request file is moved to approvals/done/<id>.json with the
outcome added, and the decision file is removed.
"""
import datetime as _dt
import glob
import json
import os
import re
import threading
import time

import guard

DIR = os.environ.get("LOCALCODE_APPROVALS_DIR") or os.path.expanduser(
    "~/Library/Application Support/LocalCode/approvals")
DONE = os.path.join(DIR, "done")
SCHEMA = "localcode.approval.v1"
POLL = 0.5
MAX_STR = 4000

APPROVE_WORDS = {"approve", "approved", "allow", "allowed", "yes", "ok", "y",
                 "accept", "承認", "許可"}
DENY_WORDS = {"deny", "denied", "reject", "rejected", "no", "n", "block",
              "却下", "拒否"}

URL_RE = re.compile(r"(?:https?|ftp|wss?)://[^\s'\"<>()\]\[`]+")
DOWNLOAD_RE = re.compile(r"(?<![\w/.])(curl|wget)\b.*?(\s-o\b|\s-O\b|--output\b|\s-P\b|\s>\s)"
                         r"|(?<![\w/.])(wget)\b|\bgit\s+clone\b|\bhf\s+download\b"
                         r"|\bhuggingface-cli\s+download\b|\bsnapshot_download\b")
INSTALL_RE = re.compile(r"\b(pip3?|uv\s+pip|npm|pnpm|yarn|brew|gem|cargo)\s+(install|add)\b")
DELETE_RE = re.compile(r"(?<![\w/.-])(rm|rmdir|unlink|trash)\s")
GIT_REMOTE_RE = re.compile(r"\bgit\s+(push|pull|fetch|clone)\b")

README = """# mightyCode for Muse 承認ブリッジ

mightyCode for Muse が承認を必要とするツール呼び出しは、GUI の承認カードと同時にこのフォルダへ
`<id>.request.json` として書き出されます（status は "pending"）。

## 判断の返し方
同じフォルダに `<id>.decision.json` を書いてください（一時ファイルに書いてから rename 推奨）。

    {"decision": "approve", "by": "Muse", "note": "ユーザー承認済み"}
    {"decision": "deny",    "by": "Muse", "note": "理由"}

decision には approve/deny のほか true/false、"承認"/"却下" も使えます。
GUI のボタンとファイルのどちらか早い方が採用されます。

## 結果
解決済みのリクエストは `done/<id>.json` に移動し、status
(approved / denied / cancelled / expired)、resolved_by (gui / muse / stop / restart)、
resolved_at が追記されます。不正な decision ファイルは `<id>.decision-error.json` に
理由を書いて削除されます（リクエストは pending のまま残ります）。

## Muse からの操作（HTTP API）
画面（http://127.0.0.1:8471/）と同じ操作を HTTP でも行えます。Muse が始めたセッションは
画面の「セッション一覧」から本人が参加でき、本人が始めたセッションに Muse が送信する
こともできます。Muse 側の呼び出しには必ず `"by": "muse"` を付けてください（画面に
送信者が表示されます）。すべて 127.0.0.1 限定、JSON、POST は Content-Type: application/json。

    GET  /api/sessions                      稼働中のセッション一覧（id, cwd, running, health, started_by）
    POST /api/session  {"cwd": "...", "by": "muse"}            セッション開始 → {"session_id"}
    POST /api/chat     {"session": id, "message": "...", "by": "muse"}   メッセージ送信（実行中は 409）
    GET  /api/events?session=id&since=N     イベント（SSE）。since は最後に受け取った seq
    GET  /api/status?session=id             接続確認（session.health の cli / model / cli_model）
    POST /api/approve  {"session": id, "call_id": "...", "decision": true, "by": "muse", "note": "..."}
    POST /api/stop     {"session": id}      実行中のターンを停止
    POST /api/check    {"session": id}      接続確認をやり直す

health.cli の detail に「macOS が…アクセス許可を求めています」と出ているときは、画面の前の
本人が macOS のダイアログで「許可」を押す必要があります（Muse からは代行できません）。
"""


def _now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_id(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:120] or "unknown"


def _trunc(v, n=MAX_STR):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"…[truncated {len(v) - n} chars]"
    if isinstance(v, dict):
        return {k: _trunc(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_trunc(x, n) for x in v[:200]]
    return v


def _atomic_write(path, obj):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_decision(obj):
    """Return (bool decision, by, note) or raise ValueError."""
    if isinstance(obj, bool):
        return obj, "muse", ""
    if isinstance(obj, str):
        obj = {"decision": obj}
    if not isinstance(obj, dict):
        raise ValueError("decision file must be a JSON object")
    d = obj.get("decision", obj.get("approved", obj.get("approve")))
    by = str(obj.get("by") or "muse")
    note = str(obj.get("note") or obj.get("reason") or "")
    if isinstance(d, bool):
        return d, by, note
    w = str(d or "").strip().lower()
    if w in APPROVE_WORDS:
        return True, by, note
    if w in DENY_WORDS:
        return False, by, note
    raise ValueError(f"unknown decision value: {d!r} (use \"approve\" or \"deny\")")


# --------------------------------------------------------------------------
# Building a request record that Muse can present as-is
# --------------------------------------------------------------------------

def _host_of(url):
    m = re.match(r"^[a-z]+://([^/:?#]+)", url, re.I)
    return m.group(1).lower() if m else ""


def describe(tool, args, cwd, reason="", cc_reason="", guard_reason=""):
    """Extract presentation fields from a tool call."""
    args = args or {}
    blob = json.dumps(args, ensure_ascii=False)
    urls = []
    for u in URL_RE.findall(blob.replace("\\/", "/")):
        u = u.rstrip(".,;:\\")
        if u not in urls:
            urls.append(u)
    hosts = []
    for u in urls:
        h = _host_of(u)
        if h and h not in hosts:
            hosts.append(h)
    command = str(args.get("command") or "") if tool == "Bash" else ""
    paths = []
    for k in ("file_path", "path", "notebook_path"):
        if args.get(k):
            paths.append(str(args[k]))

    notes = []
    category = "tool"
    if hosts:
        non_allow = [h for h in hosts if h not in guard.ALLOW_HOSTS]
        notes.append("ネットワークアクセス: " + ", ".join(hosts))
        if non_allow:
            notes.append("Guard 許可リスト外のホスト: " + ", ".join(non_allow))
        category = "network"
    if command:
        if DOWNLOAD_RE.search(command) or (tool == "Bash" and hosts and re.search(r"\b(curl|wget)\b", command)):
            notes.append("ファイルのダウンロードを含みます")
            category = "download"
        if INSTALL_RE.search(command):
            notes.append("パッケージのインストールを含みます")
            category = "install"
        if GIT_REMOTE_RE.search(command):
            notes.append("リモートリポジトリとの通信（git）を含みます")
        if DELETE_RE.search(command):
            notes.append("ファイル削除コマンドを含みます")
        if re.search(r"(^|[\s;&|])(>|>>)\s*\S", command):
            notes.append("リダイレクトによるファイル書き込みを含みます")
    if tool in ("WebFetch", "WebSearch"):
        category = "network"
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        category = "file_write"
        real_cwd = os.path.realpath(cwd or "/")
        for p in paths:
            ap = os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd or "/", p))
            if not (ap == real_cwd or ap.startswith(real_cwd + os.sep)):
                notes.append(f"作業フォルダ外への書き込み: {ap}")
            if not os.path.exists(ap):
                notes.append(f"新規作成: {ap}")

    if tool == "Bash":
        target = f"コマンド `{command}`"
    elif paths:
        target = "対象 " + ", ".join(paths)
    elif urls:
        target = "URL " + ", ".join(urls)
    else:
        target = json.dumps(args, ensure_ascii=False)[:300]
    summary = f"[{tool}] {target}"
    if hosts:
        summary += f"（アクセス先: {', '.join(hosts)}）"

    reason_parts = []
    if args.get("description"):
        reason_parts.append(f"ツール側の説明: {args['description']}")
    if reason:
        reason_parts.append(f"モデルの直前の発言: {reason}")
    if cc_reason:
        reason_parts.append(f"Claude Code が承認を求めた理由: {cc_reason}")
    return {
        "category": category,
        "command": command or None,
        "description": args.get("description") or None,
        "urls": urls,
        "hosts": hosts,
        "paths": paths,
        "reason": "\n".join(reason_parts) or "（モデルからの説明はありません）",
        "risk_notes": notes,
        "guard": {"decision": "allow",
                  "note": guard_reason or "Guard の自動ブロック対象ではありません（ユーザー判断が必要）"},
        "summary_ja": summary,
    }


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, boot_id):
        self.boot_id = boot_id
        self.lock = threading.Lock()
        self.resolvers = {}   # id -> callable(decision, by, note) -> bool
        os.makedirs(DONE, exist_ok=True)
        try:
            os.chmod(DIR, 0o700)
        except OSError:
            pass
        try:
            with open(os.path.join(DIR, "README.md"), "w", encoding="utf-8") as f:
                f.write(README)
        except OSError:
            pass
        self._expire_stale()
        threading.Thread(target=self._watch, daemon=True, name="approval-bridge").start()

    # -- paths ---------------------------------------------------------
    def request_path(self, rid):
        return os.path.join(DIR, _safe_id(rid) + ".request.json")

    def decision_path(self, rid):
        return os.path.join(DIR, _safe_id(rid) + ".decision.json")

    # -- lifecycle -----------------------------------------------------
    def _expire_stale(self):
        """Requests left by a previous backend process can never be answered."""
        for p in glob.glob(os.path.join(DIR, "*.request.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                rec = {"id": os.path.basename(p)[:-len(".request.json")]}
            if rec.get("boot_id") == self.boot_id:
                continue
            self._archive(rec, "expired", "restart",
                          "バックエンドが再起動したため、このリクエストは無効になりました")

    def publish(self, record, resolver):
        rid = record["id"]
        record = dict(record)
        record.update({
            "schema": SCHEMA, "status": "pending", "boot_id": self.boot_id,
            "created_at": record.get("created_at") or _now_iso(),
            "how_to_decide": {
                "decision_file": self.decision_path(rid),
                "approve": {"decision": "approve", "by": "Muse", "note": ""},
                "deny": {"decision": "deny", "by": "Muse", "note": "理由"},
            },
        })
        record = _trunc(record)
        with self.lock:
            self.resolvers[rid] = resolver
        _atomic_write(self.request_path(rid), record)
        return self.request_path(rid)

    def finalize(self, rid, decision, by, note=""):
        """Called once the tool call has been answered (by anyone)."""
        with self.lock:
            self.resolvers.pop(rid, None)
        rec = None
        try:
            with open(self.request_path(rid), encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            rec = {"id": rid}
        if decision is None:
            status = "cancelled"
        else:
            status = "approved" if decision else "denied"
        self._archive(rec, status, by, note)

    def _archive(self, rec, status, by, note):
        rid = rec.get("id") or "unknown"
        rec = dict(rec)
        rec.update({"status": status, "resolved_by": by,
                    "resolved_at": _now_iso(), "resolution_note": note})
        rec.pop("how_to_decide", None)
        try:
            _atomic_write(os.path.join(DONE, _safe_id(rid) + ".json"), rec)
        except OSError:
            pass
        for p in (self.request_path(rid), self.decision_path(rid)):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def pending_ids(self):
        with self.lock:
            return list(self.resolvers)

    # -- watcher -------------------------------------------------------
    def _watch(self):
        while True:
            try:
                for p in glob.glob(os.path.join(DIR, "*.decision.json")):
                    self._handle_decision_file(p)
            except Exception as e:  # never let the watcher die
                print(f"[approvals] watcher error: {e!r}", flush=True)
            time.sleep(POLL)

    def _handle_decision_file(self, p):
        rid = os.path.basename(p)[:-len(".decision.json")]
        try:
            if time.time() - os.path.getmtime(p) < 0.05:
                return  # still being written; pick it up next round
            with open(p, encoding="utf-8") as f:
                raw = f.read()
            decision, by, note = parse_decision(json.loads(raw))
        except (ValueError, json.JSONDecodeError) as e:
            self._reject_decision(rid, p, str(e))
            return
        except FileNotFoundError:
            return
        with self.lock:
            resolver = self.resolvers.get(rid)
        if resolver is None:
            if os.path.exists(os.path.join(DONE, rid + ".json")):
                msg = "このリクエストは既に解決済みです（done/ を参照）"
            else:
                msg = "該当する保留中の承認リクエストがありません"
            self._reject_decision(rid, p, msg)
            return
        if resolver(decision, by, note):
            with self.lock:
                self.resolvers.pop(rid, None)
            try:
                os.remove(p)
            except OSError:
                pass
        else:
            self._reject_decision(rid, p, "既に GUI 側で解決済みです")

    def _reject_decision(self, rid, p, msg):
        try:
            _atomic_write(os.path.join(DIR, rid + ".decision-error.json"),
                          {"id": rid, "error": msg, "at": _now_iso()})
            os.remove(p)
        except OSError:
            pass


BRIDGE = None


def init(boot_id):
    global BRIDGE
    if BRIDGE is None:
        BRIDGE = Bridge(boot_id)
    return BRIDGE
