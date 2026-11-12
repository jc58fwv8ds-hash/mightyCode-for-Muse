"""Tool implementations and their OpenAI function schemas.

Tools mirror Claude Code's core set: Bash, Read, Write, Edit, List, Glob, Grep.
Every result is a plain string; large outputs are truncated with a note.
"""
import difflib
import glob as globlib
import os
import re
import subprocess

MAX_OUT = 30000
MAX_READ = 60000

SCHEMAS = [
    {"type": "function", "function": {
        "name": "Bash",
        "description": "Run a shell command in the working directory and return stdout+stderr. Use for builds, tests, git, listing processes, etc.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command to run (zsh/bash)."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 600)."}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "Read",
        "description": "Read a text file. Returns its contents with line numbers.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based line to start from."},
            "limit": {"type": "integer", "description": "Max lines to return (default 2000)."}},
            "required": ["file_path"]}}},
    {"type": "function", "function": {
        "name": "Write",
        "description": "Write a text file (create or overwrite). Returns a summary and, for overwrites, a unified diff.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"}},
            "required": ["file_path", "content"]}}},
    {"type": "function", "function": {
        "name": "Edit",
        "description": "Replace an exact, unique string in a file with a new string. Fails if the string is missing or not unique.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"}},
            "required": ["file_path", "old_string", "new_string"]}}},
    {"type": "function", "function": {
        "name": "List",
        "description": "List files and directories at a path (default: working directory).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "Glob",
        "description": "Find files matching a glob pattern, relative to the working directory (e.g. 'src/**/*.py').",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "Grep",
        "description": "Search file contents with a regular expression. Returns matching lines with file:line prefixes.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)."}},
            "required": ["pattern"]}}},
]

NEEDS_APPROVAL = {"Bash", "Write", "Edit"}


def _trunc(text, limit=MAX_OUT):
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
    return text


def _abspath(cwd, path):
    if not path:
        return cwd
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _bash(cwd, args):
    cmd = args["command"]
    timeout = min(int(args.get("timeout") or 120), 600)
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                       text=True, timeout=timeout, executable="/bin/zsh"
                       if os.path.exists("/bin/zsh") else "/bin/bash")
    out = p.stdout or ""
    err = p.stderr or ""
    text = out + (("\n[stderr]\n" + err) if err else "")
    return _trunc(f"[exit {p.returncode}]\n{text}".strip())


def _read(cwd, args):
    path = _abspath(cwd, args["file_path"])
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or 2000)
    sel = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{offset + i}\t{ln}" for i, ln in enumerate(sel))
    return _trunc(body or "(empty file)", MAX_READ)


def preview_diff(cwd, args):
    """Unified diff an Edit/Write would produce, without applying it."""
    path = _abspath(cwd, args["file_path"])
    old = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            old = f.read()
    if "old_string" in args:  # Edit
        if args["old_string"] not in old:
            return ""
        new = old.replace(args["old_string"], args["new_string"], 1)
    else:  # Write
        new = args.get("content", "")
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                fromfile="a/" + os.path.basename(path),
                                tofile="b/" + os.path.basename(path), lineterm="")
    return _trunc("\n".join(diff), 12000)


def _write(cwd, args):
    path = _abspath(cwd, args["file_path"])
    existed = os.path.exists(path)
    diff = preview_diff(cwd, args) if existed else ""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args["content"])
    msg = f"{'Overwrote' if existed else 'Created'} {path} ({len(args['content'])} chars)."
    return msg + (("\n" + diff) if diff else "")


def _edit(cwd, args):
    path = _abspath(cwd, args["file_path"])
    with open(path, "r", encoding="utf-8") as f:
        old = f.read()
    s, n = args["old_string"], args["new_string"]
    count = old.count(s)
    if count == 0:
        return "Error: old_string not found in file. Read the file and copy the exact text."
    if count > 1:
        return f"Error: old_string appears {count} times; it must be unique. Add more context."
    diff = preview_diff(cwd, args)
    with open(path, "w", encoding="utf-8") as f:
        f.write(old.replace(s, n, 1))
    return f"Edited {path}.\n{diff}"


def _list(cwd, args):
    path = _abspath(cwd, args.get("path"))
    entries = sorted(os.listdir(path))
    out = []
    for e in entries:
        full = os.path.join(path, e)
        out.append(e + "/" if os.path.isdir(full) else e)
    return "\n".join(out) or "(empty)"


def _glob(cwd, args):
    hits = globlib.glob(args["pattern"], root_dir=cwd, recursive=True)
    hits = [h for h in sorted(hits) if os.path.isfile(os.path.join(cwd, h))]
    return _trunc("\n".join(hits[:500]) or "(no matches)")


def _grep(cwd, args):
    pat = re.compile(args["pattern"])
    base = _abspath(cwd, args.get("path"))
    files = [base] if os.path.isfile(base) else [
        os.path.join(r, f) for r, _, fs in os.walk(base) for f in fs]
    out = []
    for fp in files:
        if "/.git/" in fp or fp.endswith((".png", ".jpg", ".gif", ".zip", ".dmg")):
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if pat.search(line):
                        out.append(f"{os.path.relpath(fp, cwd)}:{i}:{line.rstrip()}")
                        if len(out) >= 300:
                            break
        except OSError:
            continue
        if len(out) >= 300:
            break
    return _trunc("\n".join(out) or "(no matches)")


_DISPATCH = {"Bash": _bash, "Read": _read, "Write": _write, "Edit": _edit,
             "List": _list, "Glob": _glob, "Grep": _grep}


def run(tool, cwd, args):
    fn = _DISPATCH.get(tool)
    if fn is None:
        return f"Error: unknown tool {tool}"
    try:
        return fn(cwd, args or {})
    except subprocess.TimeoutExpired:
        return "Error: command timed out."
    except Exception as e:  # surfaced to the model as a tool result
        return f"Error: {type(e).__name__}: {e}"
