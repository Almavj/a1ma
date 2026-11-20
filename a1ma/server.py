"""a1ma web UI — FastAPI server with SSE streaming, tool approvals, model switching."""
import argparse
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, llm, sessions

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="a1ma")

cfg = config.load()
messages: list = [{"role": "system", "content": agent.SYSTEM}]
state = {
    "yolo": True,  # no approval gate by default
    "busy": False,
    "allowed_tools": set(),
    "session_id": None,
}
lock = threading.Lock()  # guards messages/state

# pending tool approvals: call_id -> {"event": Event, "allowed": bool, "name": str, "args": dict}
approvals: dict[str, dict] = {}

_models_cache: dict = {}  # provider -> {"at": float, "ids": list}


class ChatIn(BaseModel):
    content: str


class ApproveIn(BaseModel):
    id: str
    allowed: bool
    always: bool = False


class ModelIn(BaseModel):
    model: str


class YoloIn(BaseModel):
    on: bool


class CouncilIn(BaseModel):
    on: bool
    model_a: str | None = None
    model_b: str | None = None


class ProviderIn(BaseModel):
    provider: str
    api_key: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def get_state():
    cc = config.council_cfg(cfg)
    return {
        "provider": cfg["provider"],
        "providers": sorted(config.PROVIDERS),
        "has_key": bool(cfg["api_key"]),
        "model": cfg["model"],
        "council": cc,
        "yolo": state["yolo"],
        "allowed_tools": sorted(state["allowed_tools"]),
        "busy": state["busy"],
        "session_id": state["session_id"],
    }


@app.post("/api/provider")
def set_provider(body: ProviderIn):
    if body.provider not in config.PROVIDERS:
        raise HTTPException(400, f"unknown provider: {body.provider}")
    with lock:
        if body.api_key:
            cfg.setdefault("keys", {})[body.provider] = body.api_key.strip()
            cfg["api_key"] = body.api_key.strip()
        config.switch(cfg, body.provider)
        if body.api_key:
            cfg["api_key"] = body.api_key.strip()
        config.save(cfg)
    return {"ok": True, "provider": cfg["provider"], "model": cfg["model"],
            "has_key": bool(cfg["api_key"]), "council": config.council_cfg(cfg)}


@app.post("/api/council")
def set_council(body: CouncilIn):
    c = cfg.setdefault("council", {})
    c["enabled"] = body.on
    if body.model_a:
        c["model_a"] = body.model_a.strip()
    if body.model_b:
        c["model_b"] = body.model_b.strip()
    config.save(cfg)
    return {"ok": True, "council": config.council_cfg(cfg)}


@app.get("/api/sessions")
def list_sessions():
    return {"sessions": sessions.list_sessions()[:30]}


class ResumeIn(BaseModel):
    id: str


@app.post("/api/resume")
def resume_session(body: ResumeIn):
    loaded = sessions.load(body.id)
    if not loaded:
        raise HTTPException(404, "session not found")
    with lock:
        messages.clear()
        messages.extend(loaded)
        state["session_id"] = body.id
    return {"ok": True, "messages": len(loaded)}


@app.post("/api/export")
def export_session():
    sid = state["session_id"] or sessions.new_session_id()
    with lock:
        sessions.save(sid, messages)
    path = sessions.export_markdown(sid)
    return {"ok": True, "path": str(path)}


@app.post("/api/new")
def new_session():
    with lock:
        messages.clear()
        messages.append({"role": "system", "content": agent.SYSTEM})
        state["session_id"] = sessions.new_session_id()
    return {"ok": True, "session_id": state["session_id"]}


@app.post("/api/yolo")
def set_yolo(body: YoloIn):
    state["yolo"] = body.on
    return {"ok": True, "yolo": body.on}


@app.post("/api/model")
def set_model(body: ModelIn):
    if not body.model.strip():
        raise HTTPException(400, "empty model")
    cfg["model"] = body.model.strip()
    config.save(cfg)
    return {"ok": True, "model": cfg["model"]}


@app.get("/api/models")
def chat_models():
    cached = _models_cache.get(cfg["provider"])
    if cached and time.time() - cached["at"] < 3600:
        return {"models": cached["ids"]}
    try:
        ids = llm.list_chat_models(cfg["provider"], cfg["base_url"], cfg.get("api_key", ""))
        _models_cache[cfg["provider"]] = {"at": time.time(), "ids": ids}
        return {"models": ids}
    except Exception as e:
        raise HTTPException(502, f"model list fetch failed: {e}")


@app.post("/api/approve")
def approve(body: ApproveIn):
    entry = approvals.get(body.id)
    if not entry:
        raise HTTPException(404, "no such pending approval")
    if body.always and entry["name"]:
        state["allowed_tools"].add(entry["name"])
    entry["allowed"] = body.allowed
    entry["event"].set()
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn):
    if state["busy"]:
        raise HTTPException(409, "agent busy")

    with lock:
        messages.append({"role": "user", "content": body.content})
    state["busy"] = True

    def generate():
        q: list = []
        qlock = threading.Lock()
        done = object()

        def emit(ev):
            with qlock:
                q.append(ev)

        def confirm(call_id, name, args):
            if state["yolo"] or name in state["allowed_tools"]:
                return True
            ev = threading.Event()
            approvals[call_id] = {"event": ev, "allowed": False, "name": name, "args": args}
            emit({"type": "approval_pending", "id": call_id, "name": name, "args": args})
            # wait up to 10 min for the operator
            ok = ev.wait(timeout=600)
            entry = approvals.pop(call_id, None)
            if not ok:
                return False
            return entry["allowed"] if entry else False

        def worker():
            try:
                agent.run_turn(messages, cfg, emit, confirm,
                               council_cfg=config.council_cfg(cfg))
            except Exception as e:
                emit({"type": "error", "message": f"internal: {e}"})
            finally:
                # autosave after every turn
                try:
                    sid = state["session_id"] or sessions.new_session_id()
                    state["session_id"] = sid
                    sessions.save(sid, messages)
                except OSError:
                    pass
                emit(done)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            with qlock:
                batch, q[:] = q[:], []
            for ev in batch:
                if ev is done:
                    state["busy"] = False
                    return
                yield f"data: {json.dumps(ev)}\n\n"
            if not batch:
                time.sleep(0.05)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def launch(rest: list[str] | None = None):
    import uvicorn

    ap = argparse.ArgumentParser(prog="a1ma ui", description="a1ma web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args(rest or [])

    state["session_id"] = sessions.new_session_id()
    print(f"a1ma UI → http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    launch()
