"""a1ma TUI — full-screen terminal workspace (prompt_toolkit).

Layout:
  header   provider · model · session · council/yolo state
  log      scrollable transcript (streams live)
  input    multiline; Enter sends, Esc+Enter / Ctrl-J inserts newline;
           pasted blocks stay one message
  status   working/idle · key hints · per-turn token usage
"""
import threading

from prompt_toolkit import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    Dimension, FormattedTextControl, HSplit, Layout, Window,
)
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from . import agent, config, llm, sessions

STYLE = Style.from_dict({
    "header": "bg:#11151f #7d879c",
    "brand": "bold #7aa2f7",
    "peer": "#6fd6e8",
    "model": "#a78bfa",
    "rule": "#212839",
    "log": "",
    "you": "bold #8fe3a8",
    "label": "bold #7aa2f7",
    "assistant": "#e4e8f1",
    "tool": "#6fd6e8",
    "dim": "#7d879c",
    "error": "bold #ff7a90",
    "usage": "#7d879c",
    "input": "bg:#0f121b #e4e8f1",
    "prompt": "bold #7aa2f7",
    "status": "bg:#151a26 #7d879c",
    "status.busy": "bg:#151a26 #f5c26b",
    "status.ok": "bg:#151a26 #8fe3a8",
})

HINTS = "Enter send · PgUp/PgDn or wheel: history · Esc+Enter newline · Ctrl-C quit"
HINTS_COMPACT = "Enter send · PgUp/PgDn or wheel: history · Ctrl-C quit"
HINTS_HISTORY = "scrolled back — PgDn / wheel down to follow live output"


class ChatControl(FormattedTextControl):
    """Transcript control: mouse wheel scrolls the chat history."""

    def __init__(self, tui: "Tui"):
        self.tui = tui
        super().__init__(text=tui._render, show_cursor=False)

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.tui._scroll(-3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.tui._scroll(3)
            return None
        return NotImplemented



class Tui:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session_id = sessions.new_session_id()
        self.messages = [{"role": "system", "content": agent.SYSTEM}]
        self.logical: list[list[tuple[str, str]]] = [[]]
        self.rows: list[list[tuple[str, str]]] = [[]]
        self.tail_start = 0
        self.wrap_width = 0
        self.offset = 0
        self.lock = threading.Lock()
        self.busy = False
        self.allow_all = True
        self.allowed_tools: set[str] = set()
        self.pending: dict | None = None
        self.note = ""
        self.usage_text = ""
        self.follow = True
        self._rendered_view = 0

        self.input = TextArea(
            multiline=True, wrap_lines=True, scrollbar=False,
            prompt=" you › ", height=Dimension(min=1, max=6),
            style="class:input",
            get_line_prefix=lambda ln, wrap: "" if not ln and not wrap else " " * 7,
        )

        self.nav = KeyBindings()
        empty = Condition(lambda: not self.input.text.strip())

        @self.nav.add("up", filter=empty)
        def _(event):
            self._scroll(-1)

        @self.nav.add("down", filter=empty)
        def _(event):
            self._scroll(1)

        self.input.control.key_bindings = self.nav

        self.log = Window(
            content=ChatControl(self),
            wrap_lines=False, style="class:log", dont_extend_height=False,
        )
        self.header = Window(
            content=FormattedTextControl(text=self._header), height=1,
            style="class:header",
        )
        self.status = Window(
            content=FormattedTextControl(text=self._status), height=1,
            style="class:status",
        )
        self.app = Application(
            layout=Layout(HSplit([
                self.header,
                Window(height=1, char="─", style="class:rule"),
                self.log,
                Window(height=1, char="─", style="class:rule"),
                self.input,
                self.status,
            ])),
            key_bindings=self._keys(),
            style=STYLE,
            full_screen=True,
            mouse_support=True,
        )
        self._welcome()

    # ---------- text ----------
    def _welcome(self):
        cc = config.council_cfg(self.cfg)
        self._add("class:brand", "  ◆ a1ma ")
        self._add("class:dim", "— terminal workspace\n")
        self._add("class:dim", f"  {self.cfg['provider']} · {self.cfg['model']}\n")
        self._add("class:dim", f"  session {self.session_id} · council "
                               f"{'on' if cc['enabled'] else 'off'} · tools "
                               f"{'auto' if self.allow_all else 'ask'}\n")
        self._add("class:dim", "  /help for commands\n\n")

    def _term_width(self) -> int:
        try:
            return max(20, self.app.output.get_size().columns - 1)
        except Exception:
            return 80

    def _ensure_layout(self):
        width = self._term_width()
        if width != self.wrap_width:
            self.wrap_width = width
            self._rewrap()

    def _rewrap(self):
        self.rows = []
        self.tail_start = 0
        for line in self.logical:
            self.tail_start = len(self.rows)
            self.rows.extend(self._wrap_line(line, self.wrap_width))
        if not self.rows:
            self.rows = [[]]
            self.tail_start = 0

    @staticmethod
    def _wrap_line(fragments, width: int) -> list[list[tuple[str, str]]]:
        rows: list[list[tuple[str, str]]] = []
        cur: list[tuple[str, str]] = []
        used = 0
        for style, text in fragments:
            text = text.replace("\t", "    ")
            while text:
                if used >= width:
                    rows.append(cur)
                    cur, used = [], 0
                take = text[: width - used]
                cur.append((style, take))
                used += len(take)
                text = text[len(take):]
        if cur or not rows:
            rows.append(cur)
        return rows

    def _add(self, style: str, text: str):
        text = text.replace("\t", "    ")
        with self.lock:
            self._ensure_layout()
            pieces = text.split("\n")
            for i, piece in enumerate(pieces):
                if piece:
                    self.logical[-1].append((style, piece))
                    self.rows = (self.rows[: self.tail_start]
                                 + self._wrap_line(self.logical[-1], self.wrap_width))
                if i < len(pieces) - 1:
                    self.tail_start = len(self.rows)
                    self.logical.append([])
                    self.rows.extend(self._wrap_line([], self.wrap_width))
        self._invalidate()

    def _render(self):
        with self.lock:
            self._ensure_layout()
            view = self._viewport()
            if self.follow:
                self.offset = max(0, len(self.rows) - view)
            maximum = max(0, len(self.rows) - view)
            start = max(0, min(self.offset, maximum))
            visible = self.rows[start:start + view]
        out: list[tuple[str, str]] = []
        for i, row in enumerate(visible):
            if i:
                out.append(("", "\n"))
            out.extend(row)
        if self.follow and self._rendered_view != view:
            self._rendered_view = view
            self._invalidate()
        return FormattedText(out or [("", "")])

    def _asset(self, text: str):
        self._add("class:assistant", text)

    def _invalidate(self):
        try:
            self.app.invalidate()
        except Exception:
            pass

    def _viewport(self) -> int:
        try:
            rows = self.app.output.get_size().rows
        except Exception:
            rows = 24
        height = None
        try:
            if self.log.render_info is not None:
                height = self.log.render_info.window_height
        except Exception:
            pass
        if not height:
            shown = max(1, min(6, self.input.buffer.document.line_count))
            height = max(3, rows - 5 - shown)
        return max(3, height)

    def _pin_bottom(self):
        self.follow = True
        self._invalidate()

    def _scroll(self, delta: int):
        view = self._viewport()
        with self.lock:
            self._ensure_layout()
            maximum = max(0, len(self.rows) - view)
        current = maximum if self.follow else self.offset
        new = max(0, min(maximum, current + delta))
        self.offset = new
        self.follow = new >= maximum
        self.note = "" if self.follow else HINTS_HISTORY
        self._invalidate()

    # ---------- chrome ----------
    def _header(self):
        cc = config.council_cfg(self.cfg)
        return FormattedText([
            ("class:brand", " ◆ a1ma "),
            ("class:dim", "│ "),
            ("class:peer", self.cfg["provider"]),
            ("class:dim", " · "),
            ("class:model", self.cfg["model"]),
            ("class:dim", f" · {self.session_id} · council "
                          f"{'on' if cc['enabled'] else 'off'} · yolo "
                          f"{'on' if self.allow_all else 'off'}"),
        ])

    def _status(self):
        left = ("class:status.busy", " ● working… ") if self.busy else \
               ("class:status.ok", " ● ready ")
        note = self.note or (HINTS_COMPACT if self.usage_text else HINTS)
        right = self.usage_text or ""
        return FormattedText([
            left,
            ("class:status", f" {note}  "),
            ("class:status", " │ "),
            ("class:usage", f" {right} "),
        ])

    # ---------- keys ----------
    def _keys(self):
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            self._accept_input()

        @kb.add("c-j")
        def _(event):
            # Some terminals/tmux setups deliver Enter as LF instead of CR.
            self._accept_input()

        @kb.add("escape", "enter")
        @kb.add("escape", "c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("c-c")
        def _(event):
            if self.pending is not None:
                self._resolve_approval("n")
            elif self.busy:
                self.note = "can't cancel mid-turn — wait for it to finish"
                self._invalidate()
            else:
                event.app.exit()

        @kb.add("c-d")
        def _(event):
            event.app.exit()

        @kb.add("c-l")
        def _(event):
            with self.lock:
                self.logical = [[]]
                self.rows = [[]]
                self.tail_start = 0
            self.offset = 0
            self.follow = True
            self.note = ""
            self._invalidate()

        page = lambda: max(3, self._viewport() - 2)

        @kb.add("pageup")
        def _(event):
            self._scroll(-page())

        @kb.add("pagedown")
        def _(event):
            self._scroll(page())

        @kb.add("s-up")
        def _(event):
            self._scroll(-3)

        @kb.add("s-down")
        def _(event):
            self._scroll(3)

        @kb.add("c-up")
        def _(event):
            self._scroll(-3)

        @kb.add("c-down")
        def _(event):
            self._scroll(3)

        @kb.add(Keys.ScrollUp)
        def _(event):
            self._scroll(-3)

        @kb.add(Keys.ScrollDown)
        def _(event):
            self._scroll(3)

        asking = Condition(lambda: self.pending is not None)
        for key, choice in (("y", "y"), ("n", "n"), ("a", "a"), ("A", "A")):
            @kb.add(key, filter=asking)
            def _(event, choice=choice):
                self._resolve_approval(choice)

        return kb

    def _accept_input(self):
        if self.pending is not None:
            self._resolve_approval("y")
            return
        if self.busy:
            self.note = "busy — current turn still running"
            self._invalidate()
            return
        text = self.input.text
        self.input.text = ""
        self.submit(text)

    # ---------- approvals ----------
    def _resolve_approval(self, choice: str):
        p = self.pending
        if not p:
            return
        if choice == "A":
            self.allow_all = True
            p["allowed"] = True
        elif choice == "a":
            self.allowed_tools.add(p["name"])
            p["allowed"] = True
        else:
            p["allowed"] = choice == "y"
        self.pending = None
        self.note = ""
        p["event"].set()
        self._invalidate()

    def _confirm(self, call_id, name, args) -> bool:
        if self.allow_all or name in self.allowed_tools:
            return True
        ev = threading.Event()
        self.pending = {"event": ev, "allowed": False, "name": name}
        self.note = f"allow {name}?  [y]es [n]o [a]lways [A]ll"
        self._invalidate()
        ev.wait(timeout=600)
        entry = self.pending or {"allowed": False}
        self.pending = None
        return bool(entry.get("allowed"))

    # ---------- events ----------
    def _emit(self, ev: dict):
        t = ev["type"]
        if t == "text":
            self._asset(ev["text"])
        elif t == "tool_request":
            brief = str(ev["args"])
            if len(brief) > 110:
                brief = brief[:110] + "…"
            self._add("class:tool", f"\n  ⚙ {ev['name']} ")
            self._add("class:dim", brief + "\n")
        elif t == "tool_result":
            text = (ev["result"] or "").splitlines()
            head = text[0][:100] if text else ""
            more = f" (+{len(text) - 1} lines)" if len(text) > 1 else ""
            self._add("class:dim", f"  ↳ {head}{more}\n")
        elif t == "error":
            self._add("class:error", f"\n  ✗ {ev['message']}\n")
        elif t == "turn_usage":
            u = ev["usage"]
            total = u.get("total_tokens")
            if total is None:
                self.usage_text = f"usage unavailable · {u['requests']} calls"
            else:
                self.usage_text = (f"{total} tokens · {u['requests']} calls"
                                   f" · prompt {u.get('prompt_tokens', '?')}"
                                   f" / completion {u.get('completion_tokens', '?')}")
            self._add("", "\n")
        elif t == "council_info":
            self._add("class:dim", f"  ⇄ council {ev['model_a']} × {ev['model_b']}\n")
        elif t == "council":
            if not getattr(self, "_council_head", None) == (ev["member"], ev["phase"]):
                self._council_head = (ev["member"], ev["phase"])
                self._add("class:dim", f"\n  {ev['member']} · {ev['phase']} › ")
            self._add("class:dim", ev["text"])
        elif t == "council_done":
            self._council_head = None
            self._add("", "\n")
        self._invalidate()

    # ---------- turn ----------
    def submit(self, text: str):
        text = text.strip()
        if not text:
            return
        self.note = ""
        self.follow = True
        if text.startswith("/"):
            self._slash(text)
            return
        self._add("class:you", "  you › ")
        self._add("class:assistant", text + "\n")
        self._add("class:label", "  a1ma › ")
        self.messages.append({"role": "user", "content": text})
        self.busy = True
        self.usage_text = ""
        self._invalidate()
        threading.Thread(target=self._run_turn, daemon=True).start()

    def _run_turn(self):
        try:
            agent.run_turn(self.messages, self.cfg, self._emit, self._confirm,
                           council_cfg=config.council_cfg(self.cfg))
        except Exception as e:
            self._add("class:error", f"\n  ✗ internal: {e}\n")
        finally:
            try:
                sessions.save(self.session_id, self.messages)
            except OSError:
                pass
            self.busy = False
            self.note = ""
            self._invalidate()

    # ---------- slash ----------
    def _out(self, text: str):
        self._add("class:dim", f"  {text}\n")

    def _slash(self, text: str):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()

        if cmd in ("/exit", "/quit"):
            self.app.exit()
        elif cmd == "/help":
            from .cli import SLASH
            for k, v in SLASH.items():
                if k == "/ui":
                    continue
                self._out(f"{k:22s} {v}")
        elif cmd == "/model":
            if arg:
                self.cfg["model"] = arg
                config.save(self.cfg)
                self._out(f"model → {arg}")
            else:
                self._out(f"model: {self.cfg['model']}")
        elif cmd == "/models":
            self._out("fetching models…")
            try:
                rows = llm.list_chat_models(self.cfg["provider"], self.cfg["base_url"],
                                            self.cfg.get("api_key", ""))
                for r in rows:
                    self._out(r)
            except Exception as e:
                self._out(f"fetch failed: {e}")
        elif cmd == "/provider":
            if arg:
                if arg not in config.PROVIDERS:
                    self._out(f"unknown provider — choose {', '.join(config.PROVIDERS)}")
                    return
                config.switch(self.cfg, arg)
                config.save(self.cfg)
                cc = config.council_cfg(self.cfg)
                self._out(f"provider → {self.cfg['provider']} · {self.cfg['model']}")
                self._out(f"council: {cc['model_a']} × {cc['model_b']}")
                if not self.cfg["api_key"]:
                    self._out("no API key stored for this provider")
            else:
                have = [n for n, k in (self.cfg.get("keys") or {}).items() if k]
                self._out(f"provider: {self.cfg['provider']} (keys: {', '.join(have) or 'none'})")
        elif cmd == "/new":
            try:
                sessions.save(self.session_id, self.messages)
            except OSError:
                pass
            self.session_id = sessions.new_session_id()
            self.messages = [{"role": "system", "content": agent.SYSTEM}]
            self.usage_text = ""
            self._add("class:dim", f"\n  fresh session {self.session_id}\n")
            self._invalidate()
        elif cmd == "/sessions":
            rows = sessions.list_sessions()
            if not rows:
                self._out("no saved sessions")
            for s in rows[:15]:
                self._out(f"{s['id']}  {s['messages']} msgs  {s.get('preview', '')}")
        elif cmd == "/resume":
            if not arg:
                self._out("usage: /resume <id>")
                return
            loaded = sessions.load(arg)
            if not loaded:
                self._out(f"session not found: {arg}")
                return
            self.session_id = arg
            self.messages = loaded
            self._out(f"resumed {arg} ({len(loaded)} messages)")
        elif cmd == "/undo":
            idx = next((i for i in range(len(self.messages) - 1, 0, -1)
                        if self.messages[i].get("role") == "user"), None)
            if idx is None:
                self._out("nothing to undo")
            else:
                del self.messages[idx:]
                sessions.save(self.session_id, self.messages)
                self._out("dropped last exchange")
        elif cmd == "/export":
            sessions.save(self.session_id, self.messages)
            self._out(f"exported → {sessions.export_markdown(self.session_id)}")
        elif cmd == "/status":
            chars = sum(len(m.get("content") or "") for m in self.messages)
            cc = config.council_cfg(self.cfg)
            self._out(f"provider {self.cfg['provider']} · model {self.cfg['model']}")
            self._out(f"session {self.session_id} · {len(self.messages)} messages"
                      f" · ~{chars // 4} tokens")
            self._out(f"council {'on' if cc['enabled'] else 'off'} · "
                      f"{cc['model_a']} × {cc['model_b']}")
            self._out(f"key {'set' if self.cfg.get('api_key') else 'MISSING'} · "
                      f"tools {'auto' if self.allow_all else 'ask'}")
        elif cmd == "/compact":
            removed = agent.compact(self.messages)
            sessions.save(self.session_id, self.messages)
            self._out(f"compacted, −{removed // 4} est. tokens")
        elif cmd == "/yolo":
            self.allow_all = not self.allow_all
            self._out(f"tools: {'auto-allow' if self.allow_all else 'ask'}")
        elif cmd == "/council":
            c = self.cfg.setdefault("council", {})
            if arg in ("on", "off"):
                c["enabled"] = arg == "on"
            else:
                c["enabled"] = not config.council_cfg(self.cfg)["enabled"]
            config.save(self.cfg)
            cc = config.council_cfg(self.cfg)
            self._out(f"council {'on' if cc['enabled'] else 'off'} · "
                      f"{cc['model_a']} × {cc['model_b']}")
        elif cmd == "/model2":
            c = self.cfg.setdefault("council", {})
            if arg:
                c["model_b"] = arg
                config.save(self.cfg)
                self._out(f"council model B → {arg}")
            else:
                self._out(f"council model B: {config.council_cfg(self.cfg)['model_b']}")
        elif cmd == "/ui":
            self._out("run `a1ma ui` in another terminal for the web UI")
        else:
            self._out(f"unknown command: {cmd} — /help")

    # ---------- run ----------
    def run(self):
        try:
            self.app.run()
        except (KeyboardInterrupt, EOFError):
            pass
        try:
            sessions.save(self.session_id, self.messages)
        except OSError:
            pass
        print(f"session saved: {self.session_id} — /resume {self.session_id}")


def run(cfg: dict | None = None):
    Tui(cfg or config.load()).run()
