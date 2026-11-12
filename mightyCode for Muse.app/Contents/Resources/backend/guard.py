"""Guard: pre-execution tripwire for model-issued tool calls.

Pattern-based inspection, in the spirit of mightyCode's guard.py (rewritten).
Returns (decision, reason): decision is "allow", "block".
Approval is a separate layer (the GUI asks the user); Guard blocks outright
dangerous calls before they ever reach the approval card.
"""
import os
import re

ALLOW_HOSTS = {
    "localhost", "127.0.0.1", "github.com", "huggingface.co",
    "cdn-lfs.huggingface.co", "pypi.org", "files.pythonhosted.org",
    "registry.npmjs.org", "npmjs.org", "drive.google.com",
}

NETWORK_CMD = re.compile(
    r"(?<![\w/.])(curl|wget|nc|netcat|telnet|nslookup|dig|host|"
    r"git\s+(push|clone|fetch|pull)|pip\s+install|npm\s+install|brew\s+install)\b")
SSH_CMD = re.compile(r"(^|[;&|]\s*)(ssh|scp|sftp)\s")
URL_RE = re.compile(r"https?://([^/\s'\"\)]+)")
ENCODED_PIPE = re.compile(r"(base64\s+(-d|--decode)|xxd\s+-r|openssl\s+enc\s+-d).*\|\s*(sh|bash|zsh)")
CURL_PIPE_SH = re.compile(r"(curl|wget)\b[^|]*\|\s*(sh|bash|zsh|python)")
DEV_TCP = re.compile(r"/dev/tcp/")
PY_NET = re.compile(r"\b(requests|urllib|http\.client|socket)\b")
DNS_EXFIL = re.compile(r"\b(nslookup|dig|host)\b[^|;&]*\$\(")

DESTRUCTIVE = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME)\s*$"), "rm of home or root"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+/\*"), "rm -r /*"),
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\bmkfs\b|\bdd\s+.*of=/dev/"), "disk format/dd to device"),
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/"), "chmod -R 777 /"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "power control"),
]

SECRET_PATH = re.compile(
    r"(\.ssh/|\.aws/|\.gnupg/|\.netrc|id_rsa|id_ed25519|credentials|"
    r"\.env\b|keychain|Keychains|shadow|api[_-]?key|access[_-]?token)", re.I)

PERSIST_PATH = re.compile(
    r"(\.zshrc|\.bashrc|\.bash_profile|authorized_keys|LaunchAgents|"
    r"LaunchDaemons|\.git/hooks|crontab|/etc/cron)", re.I)


def _hosts_in(text):
    return [m.group(1).split(":")[0].lower() for m in URL_RE.finditer(text)]


def check_bash(command, extra_allow_hosts=()):
    """Inspect a shell command. Returns (decision, reason)."""
    allow = ALLOW_HOSTS | {h.lower() for h in extra_allow_hosts}
    cmd = command or ""

    for pat, why in DESTRUCTIVE:
        if pat.search(cmd):
            return "block", f"destructive command: {why}"
    if ENCODED_PIPE.search(cmd) or CURL_PIPE_SH.search(cmd):
        return "block", "download/decoded content piped to a shell"
    if DNS_EXFIL.search(cmd):
        return "block", "possible DNS exfiltration"
    if DEV_TCP.search(cmd):
        return "block", "/dev/tcp network access"

    hosts = _hosts_in(cmd)
    has_net_cmd = bool(NETWORK_CMD.search(cmd)) or bool(SSH_CMD.search(cmd))
    if has_net_cmd or hosts:
        bad = [h for h in hosts if h not in allow]
        if bad:
            return "block", f"network access to non-allowlisted host: {bad[0]}"
        if has_net_cmd and not hosts and re.search(r"(?<![\w/.])(curl|wget|nc)\b", cmd) or SSH_CMD.search(cmd):
            return "block", "network command without an allowlisted URL host"

    if SECRET_PATH.search(cmd) and (has_net_cmd or hosts):
        return "block", "secret material combined with network access"
    if PERSIST_PATH.search(cmd) and re.search(r"(>>?| tee |cp |mv |install |ln )", cmd):
        return "block", "write to a persistence location"

    if PY_NET.search(cmd) and re.search(r"\bpython[3]?\b", cmd):
        bad = [h for h in hosts if h not in allow]
        if bad:
            return "block", f"python network access to non-allowlisted host: {bad[0]}"
        if "socket" in cmd:
            return "block", "raw socket use in python"
    return "allow", ""


def check_path(tool, args):
    """Inspect file-tool arguments. Returns (decision, reason)."""
    path = str(args.get("file_path") or args.get("path") or "")
    if tool in ("Read",) and SECRET_PATH.search(path):
        base = os.path.basename(path)
        if base not in ("credentials.py",):  # reading code files named credential* is fine
            return "block", f"reading a credential-like path: {path}"
    if tool in ("Write", "Edit") and PERSIST_PATH.search(path):
        return "block", f"write to a persistence location: {path}"
    return "allow", ""


def check(tool, args, extra_allow_hosts=()):
    if tool == "Bash":
        return check_bash(str(args.get("command", "")), extra_allow_hosts)
    return check_path(tool, args)
