# a1ma

Autonomous coding/security agent — opencode-style CLI + web UI on free OpenRouter models. Session-aware, gptme-inspired, with a two-model council.

## Setup

```bash
pip install -e .          # requires requests, rich, fastapi, uvicorn
```

Key is read silently from `~/.config/a1ma/config.json`, `OPENROUTER_API_KEY`, or `A1MA_API_KEY`.

## Usage

```bash
a1ma                          # interactive REPL (tool approvals OFF by default)
a1ma tui                      # full-screen terminal UI (or /tui from the REPL)
a1ma -m nvidia/nemotron-3-ultra-550b-a55b
a1ma --yolo -p "scan localhost:8080 and write a report"
a1ma --council -p "review this design"   # force council on for one run
a1ma ui                       # web UI → http://127.0.0.1:7860
a1ma ui --port 8080 --host 0.0.0.0
```

## Terminal UI

`a1ma tui` opens a full-screen workspace: header with provider/model/session/council
state, a scrollable transcript that streams tokens and tool cards live, a multiline
input (Enter sends, Esc+Enter or Ctrl-J inserts a newline — pasted blocks stay one
message), and a status bar with per-turn token usage.

- Approvals are auto-allowed at start (yolo); `/yolo` turns asking on. When a tool
  needs approval the status bar shows `allow <tool>? [y]es [n]o [a]lways [A]ll`.
- The transcript follows new output at the bottom. Scroll back through earlier
  turns with PgUp/PgDn, Shift+Up/Down, Ctrl+Up/Down, ↑/↓ while the input is
  empty, or the mouse wheel; scrolling up pauses follow (status shows
  `scrolled back`), and scrolling back to the bottom resumes it.
- Ctrl-L clears the transcript, Ctrl-C quits (or answers "no" to a pending approval).
- Same slash commands as the REPL (`/help`), including `/provider`, `/models`,
  `/new`, `/resume`, `/compact`, `/council`, `/model2`.

## Council (two-model debate)

Off by default (`/council` to enable). When on, every turn runs three phases:

1. **Draft** — model A and model B independently answer the same history (parallel).
2. **Critique** — each sees the other's draft and says what to keep/fix (parallel).
3. **Synthesis** — the primary model merges both into the final answer and runs the
   tool loop as usual; the debate packet is scrubbed from history afterward so
   context stays lean.

Cost: **5 LLM calls per turn** (2 drafts + 2 critiques + 1 synthesis) vs 1 solo.
If both council models are rate-limited/unavailable, it degrades gracefully to a
solo turn. Council models never use tools — only the synthesis phase does.

```bash
/council            # toggle on/off        /council on|off
/model2 <id>        # set council model B  (model A = /model)
/model              # shows both slots in /status
```

Web UI: provider + model pickers in the header; the council card appears only when enabled. The debate
streams live into a `⇄ council` card (A/B × draft/critique stages) above the
final answer.

Config: `"council_enabled": true` (global); model slots per provider under `council`
— unset slots default to the primary model and the first distinct fallback.

## Tools

| tool | purpose |
|---|---|
| `bash` | shell with timeout |
| `read` / `write` / `edit` | file ops (edit fails on ambiguity) |
| `glob` / `grep` | search |
| `python` | **persistent kernel** — variables/imports survive across calls, last expr returned |
| `todo` | task list for multi-step work (add/done/list/clear) |

## CLI commands

| command | action |
|---|---|
| `/help`, `/models`, `/model <id>` | introspection |
| `/new` | fresh session (autosaves current) |
| `/sessions` | list saved sessions |
| `/resume <id>` | resume a saved session |
| `/undo` | drop the last exchange (rollback a bad turn) |
| `/export` | export session to markdown |
| `/status` | model, session, context size |
| `/compact` | prune old tool outputs now |
| `/yolo` | toggle approval prompts |
| `/council [on\|off]` | toggle the two-model council |
| `/model2 [id]` | show/set council model B |
| `/ui` | launch web UI |

## Sessions

Every turn autosaves to `~/.local/share/a1ma/sessions/<id>.jsonl`; `/export` writes `<id>.md`.
The web UI has `sessions` (browse+resume) and `export` buttons in the header.

## Engine

- Reasoning disabled on the wire (`reasoning.enabled: false`) — biggest token/time saver
- Text stream coalesced into ~48-char batches (live feel, few events)
- Parallel tool execution (up to 4 concurrent calls per turn)
- Auto-compaction when context exceeds ~60k chars; old tool results pruned, anchors kept
- Context is compacted before council calls; per-turn usage aggregates every model call in the turn
- If any call reports no token totals, the turn total is shown as unavailable (never a partial sum)
- Rate-limit fallback chain: 4 free models tried in order
- Connection pooling + retries with backoff

## Providers

Two OpenAI-compatible providers, switchable at runtime — **both keys are kept**:

| provider | base URL | key | notes |
|---|---|---|---|
| `openrouter` | `openrouter.ai/api/v1` | `api_key` | free models, account-wide 50 req/day |
| `nvidia` | `integrate.api.nvidia.com/v1` | `nvidia_api_key` | trial credits, tool-capable models |

```bash
a1ma --provider nvidia        # or /provider nvidia inside the REPL
/provider                     # show active provider + which keys are stored
/models                       # chat-capable models for the active provider
```

Provider differences are handled automatically: NVIDIA uses
`stream_options.include_usage` (it rejects OpenRouter's `usage.include`), needs an
auth header on `/models`, and has no tool-support metadata — its chat list is
filtered heuristically. Verified tool-capable defaults: `nemotron-3-super-120b-a12b`,
`nemotron-3.5-lightning-30b-a3b`, `nemotron-3-ultra-550b-a55b`,
`nemotron-3-nano-omni-30b-a3b-reasoning`, `meta/muse-glimmer-30b`.

## Config

`~/.config/a1ma/config.json` — `{api_key, model, base_url, fallback_models, max_tokens, council}` (chmod 600).
Env: `OPENROUTER_API_KEY`, `A1MA_API_KEY`, `A1MA_MODEL`, `A1MA_CONFIG_DIR`, `A1MA_DATA_DIR`.
