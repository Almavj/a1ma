"""Session persistence: JSONL logs, resume, export, backtrack (gptme-style)."""
import json
import time
from pathlib import Path

from . import config

SESSION_DIR = config.DATA_DIR / "sessions"


def _ensure():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def save(session_id: str, messages: list) -> Path:
    """Atomically write the full message list for a session."""
    _ensure()
    path = SESSION_DIR / f"{session_id}.jsonl"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps({"ts": time.time(), "count": len(messages)}) + "\n")
        for m in messages:
            f.write(json.dumps(m) + "\n")
    tmp.replace(path)
    return path


def load(session_id: str) -> list:
    path = SESSION_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    messages = []
    with open(path) as f:
        for i, line in enumerate(f):
            if i == 0:
                continue  # header
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                break
    return messages


def list_sessions() -> list[dict]:
    _ensure()
    out = []
    for p in sorted(SESSION_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        head = {}
        try:
            with open(p) as f:
                head = json.loads(f.readline())
        except (json.JSONDecodeError, OSError):
            pass
        # first user message as a preview
        preview = ""
        for m in messages_preview(p):
            if m.get("role") == "user" and m.get("content"):
                preview = m["content"][:80].replace("\n", " ")
                break
        out.append({
            "id": p.stem,
            "messages": head.get("count", 0),
            "modified": head.get("ts", p.stat().st_mtime),
            "preview": preview,
        })
    return out


def messages_preview(path: Path, limit: int = 20) -> list:
    msgs = []
    try:
        with open(path) as f:
            next(f, None)
            for line in f:
                if len(msgs) >= limit:
                    break
                msgs.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    return msgs


def export_markdown(session_id: str) -> Path:
    """Export a session as readable markdown."""
    _ensure()
    messages = load(session_id)
    out = SESSION_DIR / f"{session_id}.md"
    lines = [f"# a1ma session {session_id}", ""]
    for m in messages:
        role = m.get("role", "?")
        if role == "system":
            continue
        if role == "tool":
            lines.append(f"**tool result** (`{m.get('tool_call_id', '')}`):")
            lines.append("```")
            lines.append((m.get("content") or "")[:2000])
            lines.append("```")
        elif role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                lines.append(f"**⚙ {fn.get('name')}** `{fn.get('arguments', '')[:300]}`")
            if m.get("content"):
                lines.append(m["content"])
        elif role == "assistant":
            lines.append("**a1ma:**")
            lines.append(m.get("content") or "")
        elif role == "user":
            lines.append("**you:**")
            lines.append(m.get("content") or "")
        lines.append("")
    out.write_text("\n".join(lines))
    return out


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def prune_tool_results(messages: list, keep_last: int = 8) -> int:
    """Replace old tool results with a placeholder (context saver)."""
    removed = 0
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idxs[:-keep_last]:
        c = messages[i].get("content") or ""
        if c and c != "[pruned]":
            messages[i]["content"] = "[pruned]"
            removed += len(c)
    return removed
