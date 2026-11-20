"""a1ma REPL (rich-based TUI)."""
import argparse
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from . import agent, config, llm, sessions

console = Console()

ALLOW_ALL = True  # no approval gate by default; /yolo toggles back on
ALLOWED_TOOLS: set[str] = set()

SLASH = {
    "/help": "show this help",
    "/model [id]": "show or set the model",
    "/models": "list chat-capable models for the active provider",
    "/provider [name]": "show or switch provider (openrouter | nvidia)",
    "/new": "start a fresh session",
    "/sessions": "list saved sessions",
    "/resume <id>": "resume a saved session",
    "/undo": "drop the last exchange (bad turn rollback)",
    "/export": "export current session to markdown",
    "/status": "model, session, context size",
    "/compact": "prune old tool outputs from context now",
    "/yolo": "toggle tool approval prompts",
    "/council [on|off]": "two-model council: draft → critique → synthesis",
    "/model2 [id]": "show or set the second (council) model",
    "/ui": "launch the web UI (a1ma ui)",
    "/tui": "switch to the full-screen terminal UI",
    "/exit": "quit (also Ctrl-D)",
}


def print_tool(name: str, args: dict):
    brief = json.dumps(args)
    if len(brief) > 120:
        brief = brief[:120] + "..."
    console.print(f"[bold cyan]⚙ {name}[/] [dim]{brief}[/]")


def print_result(text: str):
    lines = text.splitlines()
    shown = lines[:12]
    out = "\n".join(shown)
    if len(lines) > 12:
        out += f"\n[dim]... ({len(lines) - 12} more lines)[/]"
    console.print(Panel(out, border_style="dim", title="[dim]result[/]", title_align="left"))


def make_emit():
    last_key = [None]
    labeled = [False]

    def emit(ev: dict):
        t = ev["type"]
        if t == "text":
            if not labeled[0]:
                console.print("\n[bold blue]a1ma[/] [dim]›[/] ", end="")
                labeled[0] = True
            if last_key[0] is not None:
                console.print()
                last_key[0] = None
            console.print(ev["text"], end="", soft_wrap=True, highlight=False, markup=False)
        elif t == "error":
            console.print(f"\na1ma error: {ev['message']}\n", style="red", markup=False)
        elif t == "tool_request":
            print_tool(ev["name"], ev["args"])
        elif t == "tool_result":
            print_result(ev["result"])
        elif t == "turn_usage":
            if labeled[0]:
                console.print()
                labeled[0] = False
            usage = ev["usage"]
            total = usage.get("total_tokens")
            n = usage["requests"]
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if total is None:
                console.print(f"[token usage unavailable across {n} model calls]")
            elif prompt is not None and completion is not None:
                console.print(f"[{total} tokens across {n} model calls "
                              f"— prompt {prompt} / completion {completion}]")
            else:
                console.print(f"[{total} tokens across {n} model calls]")
        elif t == "council_info":
            console.print(f"[dim]⇄ council: {ev['model_a']}  ×  {ev['model_b']}[/]")
        elif t == "council":
            member, phase = ev["member"], ev["phase"]
            style = {"A": "magenta", "B": "cyan"}[member]
            key = (member, phase)
            if key != last_key[0]:
                if last_key[0] is not None:
                    console.print()
                console.print(f"[{style}]council {member} · {phase}[/] ", end="")
                last_key[0] = key
            console.print(ev["text"], end="", soft_wrap=True, highlight=False, markup=False)
        elif t == "council_done":
            if last_key[0] is not None:
                console.print()
                last_key[0] = None
            labeled[0] = False
            console.print("[dim]⇄ council debate complete — synthesizing final answer[/]")
    return emit


def make_confirm():
    global ALLOW_ALL

    def confirm(call_id: str, name: str, args: dict) -> bool:
        if ALLOW_ALL or name in ALLOWED_TOOLS:
            return True
        console.print(f"[yellow]allow {name}?[/] [dim](y)es / (n)o / (a)lways this tool / (A)ll[/]")
        choice = Prompt.ask(">", choices=["y", "n", "a", "A"], default="n")
        if choice == "A":
            ALLOW_ALL = True
            return True
        if choice == "a":
            ALLOWED_TOOLS.add(name)
            return True
        return choice == "y"

    return confirm


def list_models(cfg: dict):
    try:
        rows = llm.list_chat_models(cfg["provider"], cfg["base_url"], cfg.get("api_key", ""))
    except Exception as e:
        console.print(f"[red]failed to fetch models: {e}[/]")
        return
    console.print(Panel("\n".join(rows),
                        title=f"{cfg['provider']} chat models ({len(rows)})"))


def repl(cfg: dict):
    global ALLOW_ALL
    session_id = sessions.new_session_id()
    messages = [{"role": "system", "content": agent.SYSTEM}]
    console.print(Panel.fit(
        "[bold]a1ma[/] — agent online\n"
        f"provider: [cyan]{cfg['provider']}[/]  model: [cyan]{cfg['model']}[/]  "
        f"session: [cyan]{session_id}[/]\n"
        "[dim]/help for commands[/]",
        border_style="blue",
    ))
    emit, confirm = make_emit(), make_confirm()

    def autosave():
        try:
            sessions.save(session_id, messages)
        except OSError:
            pass

    while True:
        try:
            user = Prompt.ask("\n[bold green]you[/]")
        except (EOFError, KeyboardInterrupt):
            autosave()
            console.print(f"\n[dim]session saved: {session_id} — /resume {session_id} to continue[/]")
            return

        user = user.strip()
        if not user:
            continue

        if user in ("/exit", "/quit"):
            autosave()
            console.print(f"[dim]session saved: {session_id}[/]")
            return
        if user == "/tui":
            from . import tui
            autosave()
            tui.run(cfg)
            continue
        if user == "/help":
            console.print(Panel(
                "\n".join(f"[cyan]{k}[/]  {v}" for k, v in SLASH.items()),
                title="commands",
            ))
            continue
        if user == "/models":
            list_models(cfg)
            continue
        if user.startswith("/provider"):
            parts = user.split(maxsplit=1)
            if len(parts) == 2:
                name = parts[1].strip().lower()
                if name not in config.PROVIDERS:
                    console.print(f"[red]unknown provider: {name} "
                                  f"(choose from {', '.join(config.PROVIDERS)})[/]")
                    continue
                config.switch(cfg, name)
                config.save(cfg)
                cc = config.council_cfg(cfg)
                console.print(f"provider: [cyan]{cfg['provider']}[/]  model: [cyan]{cfg['model']}[/]")
                if not cfg["api_key"]:
                    console.print("[yellow]no API key set for this provider "
                                  f"(env: {config.PROVIDERS[name]['key_env'][0]} or "
                                  "edit ~/.config/a1ma/config.json)[/]")
                console.print(f"[dim]council: {cc['model_a']} × {cc['model_b']}[/]")
            else:
                active = cfg["provider"]
                have = [n for n, k in (cfg.get("keys") or {}).items() if k]
                console.print(f"active provider: [cyan]{active}[/]  "
                              f"(keys stored for: {', '.join(have) or 'none'})")
            continue
        if user.startswith("/model"):
            parts = user.split(maxsplit=1)
            if len(parts) == 2:
                cfg["model"] = parts[1].strip()
                config.save(cfg)
                console.print(f"model set to [cyan]{cfg['model']}[/]")
            else:
                console.print(f"current model: [cyan]{cfg['model']}[/]")
            continue
        if user.startswith("/council"):
            parts = user.split(maxsplit=1)
            c = cfg.setdefault("council", {})
            if len(parts) == 2 and parts[1] in ("on", "off"):
                c["enabled"] = parts[1] == "on"
            else:
                c["enabled"] = not config.council_cfg(cfg)["enabled"]
            config.save(cfg)
            cc = config.council_cfg(cfg)
            state = "on" if cc["enabled"] else "off"
            console.print(f"council: [bold]{state}[/]  "
                          f"[cyan]{cc['model_a']}[/] × [cyan]{cc['model_b']}[/]")
            continue
        if user.startswith("/model2"):
            parts = user.split(maxsplit=1)
            c = cfg.setdefault("council", {})
            if len(parts) == 2:
                c["model_b"] = parts[1].strip()
                config.save(cfg)
                console.print(f"council model B set to [cyan]{c['model_b']}[/]")
            else:
                cc = config.council_cfg(cfg)
                console.print(f"council model B: [cyan]{cc['model_b']}[/]")
            continue
        if user == "/new":
            autosave()
            session_id = sessions.new_session_id()
            messages = [{"role": "system", "content": agent.SYSTEM}]
            console.print(f"[dim]fresh session {session_id}[/]")
            continue
        if user == "/sessions":
            rows = sessions.list_sessions()
            if not rows:
                console.print("[dim]no saved sessions[/]")
            else:
                lines = [
                    f"[cyan]{s['id']}[/]  {s['messages']} msgs  {s['preview']}"
                    for s in rows[:15]
                ]
                console.print(Panel("\n".join(lines), title="sessions"))
            continue
        if user.startswith("/resume"):
            parts = user.split(maxsplit=1)
            if len(parts) != 2:
                console.print("[red]usage: /resume <id>[/]")
                continue
            loaded = sessions.load(parts[1].strip())
            if not loaded:
                console.print(f"[red]session not found: {parts[1]}[/]")
                continue
            session_id = parts[1].strip()
            messages = loaded
            console.print(f"[dim]resumed {session_id} ({len(messages)} messages)[/]")
            continue
        if user == "/undo":
            # drop back to before the last user message
            idx = None
            for i in range(len(messages) - 1, 0, -1):
                if messages[i].get("role") == "user":
                    idx = i
                    break
            if idx is None:
                console.print("[dim]nothing to undo[/]")
            else:
                dropped = len(messages) - idx
                del messages[idx:]
                autosave()
                console.print(f"[dim]dropped last exchange ({dropped} messages)[/]")
            continue
        if user == "/export":
            autosave()
            path = sessions.export_markdown(session_id)
            console.print(f"[dim]exported → {path}[/]")
            continue
        if user == "/status":
            chars = sum(len(m.get("content") or "") for m in messages)
            cc = config.council_cfg(cfg)
            console.print(Panel(
                f"provider: [cyan]{cfg['provider']}[/]\n"
                f"model: [cyan]{cfg['model']}[/]\n"
                f"api key: [bold]{'set' if cfg['api_key'] else 'MISSING'}[/]\n"
                f"council: [bold]{'on' if cc['enabled'] else 'off'}[/]  "
                f"{cc['model_a']} × {cc['model_b']}\n"
                f"session: [cyan]{session_id}[/]\n"
                f"messages: {len(messages)}\n"
                f"context: ~{chars // 4} tokens (est)\n"
                f"approvals: [bold]{'OFF (yolo)' if ALLOW_ALL else 'ON'}[/]",
                title="status",
            ))
            continue
        if user == "/compact":
            removed = agent.compact(messages)
            console.print(f"[dim]compacted, −{removed // 4} est. tokens[/]")
            autosave()
            continue
        if user == "/yolo":
            ALLOW_ALL = not ALLOW_ALL
            console.print(f"tool approvals: [bold]{'OFF (auto-allow)' if ALLOW_ALL else 'ON'}[/]")
            continue
        if user == "/ui":
            from . import server
            server.launch()
            continue

        messages.append({"role": "user", "content": user})
        try:
            agent.run_turn(messages, cfg, emit, confirm,
                           council_cfg=config.council_cfg(cfg))
        except KeyboardInterrupt:
            console.print("\n[yellow]interrupted[/]")
        autosave()


def run_once(cfg: dict, prompt: str, yolo: bool, council: bool | None = None):
    emit = make_emit()
    confirm = (lambda cid, n, a: True) if yolo else make_confirm()
    messages = [
        {"role": "system", "content": agent.SYSTEM},
        {"role": "user", "content": prompt},
    ]
    if council is not None:
        cfg.setdefault("council", {})["enabled"] = council
    agent.run_turn(messages, cfg, emit, confirm, council_cfg=config.council_cfg(cfg))
    session_id = sessions.new_session_id()
    sessions.save(session_id, messages)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "ui":
        from . import server
        server.launch(rest=argv[1:])
        return
    if argv and argv[0] == "tui":
        from . import tui
        tui.run()
        return

    ap = argparse.ArgumentParser(prog="a1ma", description="a1ma agent")
    ap.add_argument("-m", "--model", help="model id (openrouter)")
    ap.add_argument("-p", "--prompt", help="run a single prompt non-interactively")
    ap.add_argument("--yolo", action="store_true", help="auto-allow all tool calls")
    ap.add_argument("--council", action="store_true",
                    help="two-model council mode (draft → critique → synthesis)")
    ap.add_argument("--provider", choices=sorted(config.PROVIDERS),
                    help="API provider (openrouter | nvidia)")
    args = ap.parse_args()

    global ALLOW_ALL
    if args.yolo:
        ALLOW_ALL = True

    cfg = config.load()
    if args.provider:
        config.switch(cfg, args.provider)
        config.save(cfg)
    if args.model:
        cfg["model"] = args.model

    if args.prompt:
        run_once(cfg, args.prompt, args.yolo, council=True if args.council else None)
        return
    if args.council:
        cfg.setdefault("council", {})["enabled"] = True

    repl(cfg)


if __name__ == "__main__":
    main()
