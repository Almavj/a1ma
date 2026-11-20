"""Council mode: two models discuss each turn and synthesize a final answer.

Flow per user turn:
  1. Model A and Model B independently draft an answer (parallel, same history).
  2. Each sees the other's draft and writes a short critique/refinement.
  3. The primary model reads both drafts + both critiques and streams the
     final answer (with tool access, continuing the normal agent loop).

Emits:
  {"type":"council","phase":"draft|critique","member":"A|B","model":str,"text":str}
  {"type":"council_done","model_a":str,"model_b":str}
  plus normal agent events from the synthesis phase.
"""
from concurrent.futures import ThreadPoolExecutor

from . import llm

CRITIQUE_ROUNDS = 1  # one exchange of drafts, then synthesis


def _collect(cfg: dict, model: str, messages: list, emit, member: str, phase: str) -> str:
    """Run one full (non-tool) completion, streaming it as council events."""
    parts: list[str] = []
    try:
        for ev in llm.stream_chat(cfg["api_key"], cfg["base_url"], model, messages, tools=None,
                                  max_tokens=cfg.get("max_tokens")):
            if ev["type"] == "text":
                parts.append(ev["text"])
                emit({"type": "council", "phase": phase, "member": member,
                      "model": model, "text": ev["text"]})
            elif ev["type"] == "usage":
                emit({"type": "usage", "usage": ev["usage"]})
    except llm.LLMError as e:
        msg = f"[{member} unavailable: {e}]"
        parts.append(msg)
        emit({"type": "council", "phase": phase, "member": member, "model": model, "text": msg})
    return "".join(parts)


def _role_prefix(member: str) -> str:
    return f"You are council member {member} of a two-model review panel inside the a1ma agent. "


def _draft(cfg: dict, model: str, messages: list, emit, member: str) -> str:
    msgs = [{"role": "system", "content": _role_prefix(member) +
             "Answer the operator's latest message as a complete, high-quality standalone draft. "
             "No preamble about being a panel member — just the answer."},
            *messages[1:]]  # same history minus the primary system prompt
    return _collect(cfg, model, msgs, emit, member, "draft")


def _critique(cfg: dict, model: str, history: list, own_draft: str, other_draft: str,
              emit, member: str, other_member: str) -> str:
    msgs = [{"role": "system", "content": _role_prefix(member) +
             "You are now in a critique round. Improve the joint answer."},
            *history[1:],
            {"role": "assistant", "content": own_draft},
            {"role": "user", "content":
             f"Council member {other_member} produced this competing draft:\n\n{other_draft}\n\n"
             f"Compare it with yours. Briefly: where is it better, where is it wrong or missing "
             f"something, and what should the final answer keep from each? Max 150 words, no fluff."}]
    return _collect(cfg, model, msgs, emit, member, "critique")


def run_council(messages: list, cfg: dict, model_a: str, model_b: str, emit) -> str:
    """Run draft + critique rounds; return the debate packet for synthesis.
    Returns '' if both models failed (caller should fall back to solo mode)."""
    history = list(messages)
    emit({"type": "council_info", "model_a": model_a, "model_b": model_b})

    # phase 1: parallel independent drafts on the same history
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_draft, cfg, model_a, history, emit, "A")
        fb = pool.submit(_draft, cfg, model_b, history, emit, "B")
        draft_a, draft_b = fa.result(), fb.result()

    if draft_a.startswith("[A unavailable") and draft_b.startswith("[B unavailable"):
        return ""  # nothing worked — let solo path handle the turn

    # phase 2: each critiques the other (sequential rounds keep tokens sane)
    for _ in range(CRITIQUE_ROUNDS):
        with ThreadPoolExecutor(max_workers=2) as pool:
            ca = pool.submit(_critique, cfg, model_a, history, draft_a, draft_b, emit, "A", "B")
            cb = pool.submit(_critique, cfg, model_b, history, draft_b, draft_a, emit, "B", "A")
            critique_a, critique_b = ca.result(), cb.result()

    return f"""COUNCIL DEBATE (member A = {model_a}, member B = {model_b})

Member A draft:
{draft_a}

Member B draft:
{draft_b}

Member A critique:
{critique_a}

Member B critique:
{critique_b}"""
