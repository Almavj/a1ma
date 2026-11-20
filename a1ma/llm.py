"""OpenAI-compatible streaming client for OpenRouter — tuned for speed."""
import json
import threading
import time
import requests

TIMEOUT = (10, 300)
CONNECT_RETRIES = 3
FLUSH_CHARS = 48  # coalesce stream deltas into ~sentence-sized SSE events

# thread-local sessions: safe for parallel council calls, keeps pooling
_tls = threading.local()


def _session() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8))
        _tls.s = s
    return s


class LLMError(Exception):
    pass


class RateLimitError(LLMError):
    pass


def stream_chat(api_key: str, base_url: str, model: str, messages: list,
                tools: list | None = None, max_tokens: int | None = None):
    """Yield {"type":"text"|"tool_calls"|"usage", ...} events as the stream arrives."""
    nvidia = "nvidia.com" in base_url
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # biggest speed/token win: free models waste most completion tokens on
        # hidden reasoning chains; turn it off (both providers honor this)
        "reasoning": {"enabled": False},
    }
    if nvidia:
        # openai-compat dialects differ: nvidia rejects `usage.include`
        payload["stream_options"] = {"include_usage": True}
    else:
        payload["usage"] = {"include": True}
    if tools:
        payload["tools"] = tools
    if max_tokens:
        payload["max_tokens"] = max_tokens

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    if not nvidia:
        headers["HTTP-Referer"] = "https://github.com/a1ma"
        headers["X-Title"] = "a1ma"

    resp = None
    for attempt in range(CONNECT_RETRIES):
        try:
            resp = _session().post(
                f"{base_url}/chat/completions",
                headers=headers, json=payload, stream=True, timeout=TIMEOUT,
            )
            break
        except requests.RequestException as e:
            if attempt == CONNECT_RETRIES - 1:
                raise LLMError(f"connection failed after {CONNECT_RETRIES} attempts: {e}") from e
            time.sleep(1.5 ** attempt)

    if resp.status_code == 429:
        raise _rate_limit(resp)
    if resp.status_code >= 500:
        # one quick retry for transient upstream errors
        time.sleep(1)
        try:
            resp = _session().post(
                f"{base_url}/chat/completions",
                headers=headers, json=payload, stream=True, timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise LLMError(f"connection failed: {e}") from e
        if resp.status_code == 429:
            raise _rate_limit(resp)
    if resp.status_code != 200:
        try:
            body = resp.json()
            # openrouter: {"error":{"message":...}}  nvidia: {"message":...}
            detail = (body.get("error", {}).get("message")
                      or body.get("message") or resp.text[:500])
        except Exception:
            detail = resp.text[:500]
        if "rate limit" in detail.lower() or resp.status_code == 429:
            raise RateLimitError(detail[:300])
        raise LLMError(f"HTTP {resp.status_code}: {detail}")

    tool_acc: dict[int, dict] = {}
    usage = None
    buf = ""

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            msg = str(chunk["error"])
            if "rate limit" in msg.lower():
                raise RateLimitError(msg[:300])
            raise LLMError(msg[:500])
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                buf += delta["content"]
                # flush in small batches: live feel with few SSE events
                if len(buf) >= FLUSH_CHARS or "\n" in buf:
                    yield {"type": "text", "text": buf}
                    buf = ""
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    acc["name"] += fn["name"]
                if fn.get("arguments"):
                    acc["arguments"] += fn["arguments"]

    if buf:
        yield {"type": "text", "text": buf}
    if tool_acc:
        calls = []
        for idx in sorted(tool_acc):
            a = tool_acc[idx]
            try:
                args = json.loads(a["arguments"]) if a["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": a["arguments"]}
            calls.append({"id": a["id"] or f"call_{idx}", "name": a["name"], "arguments": args})
        yield {"type": "tool_calls", "calls": calls}
    # always emitted: an empty dict means "this call reported no usage", which
    # lets the agent count the call and mark the turn total as unavailable
    yield {"type": "usage", "usage": usage or {}}


def _rate_limit(resp) -> RateLimitError:
    try:
        body = resp.json()
        detail = (body.get("error", {}).get("message")
                  or body.get("message") or "rate limited")
    except Exception:
        detail = "rate limited"
    return RateLimitError(detail[:300])


NON_CHAT_HINTS = (
    "embed", "clip", "parse", "video", "guard", "reward", "retriever",
    "detector", "deplot", "synthetic", "calibration",
)


def list_chat_models(provider: str, base_url: str, api_key: str = "") -> list[str]:
    """Model ids that can plausibly drive the agent, per provider."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = _session().get(f"{base_url}/models", headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()["data"]
    if provider == "nvidia":
        # flat grab-bag, no capability metadata: heuristically drop non-chat
        return sorted(
            m["id"] for m in data
            if not any(h in m["id"].lower() for h in NON_CHAT_HINTS)
        )
    # openrouter: free + tool-capable only
    return sorted(
        m["id"] for m in data
        if m["id"].endswith(":free")
        and "tools" in (m.get("supported_parameters") or [])
    )
