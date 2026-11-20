"""Built-in tools: bash, read, write, edit, glob, grep."""
import fnmatch
import glob as globmod
import os
import re
import subprocess
from pathlib import Path

CWD = Path.cwd()


def _clip(text: str, limit: int = 40000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [clipped, {len(text)} chars total]"


def run_bash(command: str, timeout: int = 120) -> str:
    try:
        p = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=CWD,
        )
        out = p.stdout
        if p.stderr:
            out += ("\n" if out else "") + p.stderr
        out += f"\n[exit {p.returncode}]" if out else f"[exit {p.returncode}]"
        return _clip(out or "[no output]")
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"
    except Exception as e:
        return f"[error: {e}]"


def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    p = (CWD / path).resolve()
    if not p.exists():
        return f"error: file not found: {path}"
    if p.is_dir():
        return f"error: {path} is a directory"
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError as e:
        return f"error: {e}"
    seg = lines[offset : offset + limit]
    if not seg:
        return "[empty / offset beyond EOF]"
    width = len(str(offset + len(seg)))
    numbered = [f"{offset + i + 1:>{width}}| {l}" for i, l in enumerate(seg)]
    return _clip("\n".join(numbered))


def write_file(path: str, content: str) -> str:
    p = (CWD / path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} chars to {path}"


def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = (CWD / path).resolve()
    if not p.exists():
        return f"error: file not found: {path}"
    text = p.read_text()
    n = text.count(old)
    if n == 0:
        return "error: old_string not found"
    if n > 1 and not replace_all:
        return f"error: old_string matches {n} times; pass replace_all or add context"
    text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    p.write_text(text)
    return f"edited {path} ({n if replace_all else 1} replacement(s))"


def do_glob(pattern: str) -> str:
    matches = sorted(globmod.glob(str(CWD / pattern), recursive=True))
    rel = [os.path.relpath(m, CWD) for m in matches[:200]]
    if not rel:
        return "no matches"
    out = "\n".join(rel)
    if len(matches) > 200:
        out += f"\n... [{len(matches)} total]"
    return out


def do_grep(pattern: str, file_glob: str = "**/*") -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"error: bad regex: {e}"
    results = []
    for f in globmod.glob(str(CWD / file_glob), recursive=True):
        if not os.path.isfile(f):
            continue
        try:
            with open(f, errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        rel = os.path.relpath(f, CWD)
                        results.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(results) >= 200:
                            return _clip("\n".join(results) + "\n... [truncated]")
        except OSError:
            continue
    return "\n".join(results) if results else "no matches"


# --- tool schemas for the model ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the working directory. Returns stdout+stderr and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a text file with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "0-based line offset"},
                    "limit": {"type": "integer", "description": "max lines (default 2000)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write content to a file (creates parent dirs, overwrites).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace old_string with new_string in a file. Fails if not found or ambiguous (unless replace_all).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Glob files under the working directory (supports **).",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Regex-search file contents. Returns path:line: text matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "file_glob": {"type": "string", "description": "glob filter, default **/*"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "python",
            "description": "Execute Python in a persistent kernel — variables/imports survive across calls. Last expression's value is returned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "restart": {"type": "boolean", "description": "restart kernel, clearing state"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo",
            "description": "Maintain a task list for multi-step work. action: add, done, list, clear.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "done", "list", "clear"]},
                    "text": {"type": "string", "description": "task text (add)"},
                    "index": {"type": "integer", "description": "task number (done)"},
                },
                "required": ["action"],
            },
        },
    },
]


def todo_store_path():
    from . import config
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "todos.json"


def do_todo(action: str, text: str = "", index: int | None = None) -> str:
    import json
    p = todo_store_path()
    todos: list = json.loads(p.read_text()) if p.exists() else []
    if action == "add":
        if not text.strip():
            return "error: empty task"
        todos.append({"text": text.strip(), "done": False})
        p.write_text(json.dumps(todos, indent=1))
        return f"added #{len(todos)}: {text.strip()}"
    if action == "done":
        if index is None or not (1 <= index <= len(todos)):
            return f"error: index must be 1..{len(todos)}"
        todos[index - 1]["done"] = True
        p.write_text(json.dumps(todos, indent=1))
        return f"completed #{index}: {todos[index - 1]['text']}"
    if action == "list":
        if not todos:
            return "[no tasks]"
        lines = [
            f"{'[x]' if t['done'] else '[ ]'} {i + 1}. {t['text']}"
            for i, t in enumerate(todos)
        ]
        return "\n".join(lines)
    if action == "clear":
        p.write_text("[]")
        return "todos cleared"
    return f"error: unknown action {action}"


def dispatch(name: str, args: dict) -> str:
    if name == "bash":
        return run_bash(args["command"], args.get("timeout", 120))
    if name == "read":
        return read_file(args["path"], args.get("offset", 0), args.get("limit", 2000))
    if name == "write":
        return write_file(args["path"], args["content"])
    if name == "edit":
        return edit_file(args["path"], args["old"], args["new"], args.get("replace_all", False))
    if name == "glob":
        return do_glob(args["pattern"])
    if name == "grep":
        return do_grep(args["pattern"], args.get("file_glob", "**/*"))
    if name == "python":
        from . import python_kernel
        if args.get("restart"):
            return python_kernel.python_restart()
        return python_kernel.python_exec(args["code"])
    if name == "todo":
        return do_todo(args["action"], args.get("text", ""), args.get("index"))
    return f"error: unknown tool {name}"
