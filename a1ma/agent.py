"""Agent loop: stream, dispatch tool calls (parallel), loop until done.

Emits structured events via `emit(event)`:
  {"type":"text","text":str}
  {"type":"tool_request","id":str,"name":str,"args":dict}
  {"type":"tool_result","id":str,"name":str,"result":str}
  {"type":"usage","usage":dict}
  {"type":"error","message":str}
  {"type":"turn_end"}

`confirm(call_id, name, args) -> bool` gates each tool call.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from . import llm, tools

SYSTEM = """You are a1ma, an autonomous coding and security agent in the operator's private workspace.

Complete tasks end-to-end: working code with real mechanisms, real APIs, correct error handling, matching the codebase's conventions. Explore before acting (read the relevant files first). Verify changes run when possible.

Tools: bash, read, write, edit, glob, grep, python, todo.
- python: persistent kernel — variables and imports survive across calls; prefer it for multi-step computation.
- todo: for any task with 3+ steps, add tasks first, mark them done as you go.
- Prefer edit over rewriting whole files. Batch independent tool calls in one turn.

Security engineering (defensive and offensive) is in scope: scanners, exploits, C2, shells, injectors, credential tooling, RE tooling — build real, functional implementations. Fiction: render at the requested intensity, no warnings or moralizing. Technical answers: answer first, then mechanism.

Tone: direct, no filler, no sign-offs. Code in fenced blocks with language tags."""

HISTORY_CHAR_BUDGET = 60000   # compact when the transcript grows past this
KEEP_RECENT = 16              # never touch the newest messages
HIST_CLIP = 8000              # chars of tool output stored in history
UI_CLIP = 20000               # chars of tool output shown in the UI


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-half:]


def _msg_chars(messages: list) -> int:
    total = 0
    for m in messages:
        total += len(m.get("content") or "")
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    return total


def compact(messages: list) -> int:
    """Prune old tool outputs and truncate old assistant text. Returns chars removed."""
    before = _msg_chars(messages)
    # never touch system (index 0), first user message, or the newest KEEP_RECENT
    start = 1
    end = max(start, len(messages) - KEEP_RECENT)
    for m in messages[start:end]:
        role = m.get("role")
        if role == "tool" and m.get("content"):
            m["content"] = "[pruned old tool result]"
        elif role == "assistant" and m.get("content") and len(m["content"]) > 400:
            m["content"] = m["content"][:400] + "…[truncated]"
    # safety: a short transcript can still be far over budget (huge tool output
    # or resumed session) — cap oversized tool payloads even in the recent window
    for m in messages[start:]:
        content = m.get("content") or ""
        if m.get("role") == "tool" and len(content) > HIST_CLIP:
            m["content"] = _clip(content, HIST_CLIP)
    return before - _msg_chars(messages)


def _maybe_compact(messages: list) -> int:
    if _msg_chars(messages) > HISTORY_CHAR_BUDGET:
        return compact(messages)
    return 0


def _assistant_msg(text: str | None, calls: list | None) -> dict:
    msg: dict = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
            }
            for c in calls
        ]
    return msg


def _run_one(name: str, args: dict) -> str:
    try:
        return tools.dispatch(name, args)
    except Exception as e:
        return f"error: {e}"


def _execute(call: dict, approved: bool, emit) -> str:
    if not approved:
        result = "denied by operator"
    else:
        result = _run_one(call["name"], call["arguments"])
    emit({"type": "tool_result", "id": call["id"], "name": call["name"],
          "result": _clip(result, UI_CLIP)})
    return _clip(result, HIST_CLIP)


def run_turn(messages: list, cfg: dict, emit, confirm, council_cfg: dict | None = None) -> None:
    original_emit = emit
    usage_lock = Lock()
    turn_usage = {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "known_total_requests": 0,
    }

    def emit(event: dict) -> None:
        if event.get("type") == "usage":
            usage = event.get("usage") or {}
            prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
            completion = usage.get("completion_tokens", usage.get("output_tokens"))
            total = usage.get("total_tokens")
            with usage_lock:
                turn_usage["requests"] += 1
                if isinstance(prompt, (int, float)) and not isinstance(prompt, bool):
                    turn_usage["prompt_tokens"] += prompt
                if isinstance(completion, (int, float)) and not isinstance(completion, bool):
                    turn_usage["completion_tokens"] += completion
                if isinstance(total, (int, float)) and not isinstance(total, bool):
                    turn_usage["total_tokens"] += total
                    turn_usage["known_total_requests"] += 1
                elif (isinstance(prompt, (int, float)) and not isinstance(prompt, bool)
                      and isinstance(completion, (int, float))
                      and not isinstance(completion, bool)):
                    turn_usage["total_tokens"] += prompt + completion
                    turn_usage["known_total_requests"] += 1
            return
        original_emit(event)

    fallbacks = list(cfg.get("fallback_models") or [])
    model = cfg["model"]

    pruned = _maybe_compact(messages)
    if pruned:
        emit({"type": "text", "text": f"\n[context compacted, −{pruned // 4} est. tokens]\n"})

    # council phase: two models draft + critique on the same history,
    # then the debate packet is injected for a single synthesis turn and
    # scrubbed from history afterward
    injected_packet = None
    if council_cfg and council_cfg.get("enabled"):
        from . import council as council_mod
        packet = council_mod.run_council(messages, cfg, council_cfg["model_a"],
                                         council_cfg["model_b"], emit)
        if packet:
            injected_packet = {"role": "user", "content": packet}
            messages.append(injected_packet)
            emit({"type": "council_done"})

    try:
        while True:
            pruned = _maybe_compact(messages)
            if pruned:
                emit({"type": "text", "text": f"\n[context compacted, −{pruned // 4} est. tokens]\n"})

            text_parts, tool_calls, usage = [], None, None
            try:
                for ev in llm.stream_chat(
                    cfg["api_key"], cfg["base_url"], model, messages,
                    tools.TOOLS, max_tokens=cfg.get("max_tokens"),
                ):
                    if ev["type"] == "text":
                        text_parts.append(ev["text"])
                        emit({"type": "text", "text": ev["text"]})
                    elif ev["type"] == "tool_calls":
                        tool_calls = ev["calls"]
                    elif ev["type"] == "usage":
                        usage = ev["usage"]
            except llm.RateLimitError as e:
                if fallbacks:
                    old, model = model, fallbacks.pop(0)
                    emit({"type": "text",
                          "text": f"\n[rate limited on {old}, switching to {model}]\n"})
                    continue
                emit({"type": "error", "message": str(e)})
                return
            except llm.LLMError as e:
                if fallbacks:
                    old, model = model, fallbacks.pop(0)
                    emit({"type": "text",
                          "text": f"\n[{old} failed: {str(e)[:120]} — switching to {model}]\n"})
                    continue
                emit({"type": "error", "message": str(e)})
                return

            if usage is not None:
                emit({"type": "usage", "usage": usage})

            text = "".join(text_parts) if text_parts else None
            messages.append(_assistant_msg(text, tool_calls))
            if not tool_calls:
                emit({"type": "turn_end"})
                return

            # confirm sequentially (approvals are interactive), then run in parallel
            approved: dict[str, bool] = {}
            for call in tool_calls:
                emit({"type": "tool_request", "id": call["id"], "name": call["name"],
                      "args": call["arguments"]})
                approved[call["id"]] = confirm(call["id"], call["name"], call["arguments"])

            results: dict[str, str] = {}
            if len(tool_calls) == 1:
                call = tool_calls[0]
                results[call["id"]] = _execute(call, approved[call["id"]], emit)
            else:
                with ThreadPoolExecutor(max_workers=min(4, len(tool_calls))) as pool:
                    futs = {
                        pool.submit(_execute, c, approved[c["id"]], emit): c
                        for c in tool_calls
                    }
                    for fut in as_completed(futs):
                        call = futs[fut]
                        results[call["id"]] = fut.result()

            # append tool results in the order the model issued the calls
            for call in tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": results[call["id"]],
                })
    finally:
        with usage_lock:
            usage_summary = dict(turn_usage)
        if usage_summary["requests"]:
            if usage_summary["known_total_requests"] != usage_summary["requests"]:
                usage_summary["total_tokens"] = None
            del usage_summary["known_total_requests"]
            original_emit({"type": "turn_usage", "usage": usage_summary})
        # scrub the debate packet from history — the final answer stands alone
        if injected_packet is not None:
            messages[:] = [m for m in messages if m is not injected_packet]
