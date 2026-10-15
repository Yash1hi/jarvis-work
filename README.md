# Jarvis (work)

Say "Hey Jarvis" and talk. Jarvis is the voice; background Claude Code agents do the work, with the same
setup as your terminal sessions (MCP servers, plugins, skills, CLIs). You keep talking while they run,
and Jarvis tells you when one finishes or has a question.

This is the work version of a home Jarvis, with the TV, Home Assistant, Plex, Arc and other home
parts removed.

```
mic ─► aecio (Apple echo cancellation) ─► "hey jarvis" (openWakeWord, local)
                                              │ opens
                                              ▼
                         gpt-realtime-2.1 session (voice cedar) ── tools ──► agents.py
                                              ▲                                │ claude --bg / codex exec
                       relay: "tell Yash ..." │                                ▼
                                              └────── watcher ◄──── background agents (your Claude Code setup)
```

## Run

```sh
security add-generic-password -U -a "$USER" -s jarvis-openai-api-key -w "$(pbpaste)"   # copy the OpenAI key first; stores it in the Keychain
uv run python -m jarvisd           # foreground; --mini for the cheaper model, --threshold 0.4 for an eager wake word
launchd/install.sh                 # or as a login agent, with the Jarvis Controls menu-bar switch
```

The first run asks for microphone access. Typing into your Terminal sessions also needs
Automation > Terminal permission.

## What it can do

| Tool | What it does |
|---|---|
| `agent_start(task, project?, agent?)` | Hands off a task and returns at once. Without `project` it's an errand, run in `workspace/` with a brief (no sending, posting, merging, deploying or spending unless asked). With `project` it works in that repo (folders under `~/Scorecard`, `~/Code`, or `jarvis`; change the roots with `JARVIS_PROJECT_ROOTS`). |
| `agents` | Background agents plus your open Claude Code and Codex Terminal sessions |
| `agent_result` | An agent's full last message |
| `agent_send` | A follow-up or an answer. It resumes background agents, and types into your Terminal tabs only after you confirm out loud. |
| `agent_open` / `agent_stop` | Shows a session in Terminal, or stops a background one |
| `show(title, markdown \| session \| url, size?)` | Puts something on the workspace panel: markdown (with images, video, PDFs and HTML charts as `![caption](path or URL)`), an agent's full answer, or one file or web page |
| `workspace(action)` | Shows, hides, clears or resizes the panel, or changes its opacity (absolute or relative), and returns its state |
| `spending`, `get_time`, `end_conversation` | |

When an agent finishes, the watcher speaks its first sentence, or its closing question if it asks one.
Agents that end on a question show up as `blocked`. Announcements also post a macOS notification.
During a conversation, the live session passes the news on. Otherwise Jarvis speaks it with
`gpt-4o-mini-tts`.

## The workspace panel

A translucent glass panel docked in the top-right corner. It sits on every desktop and over full-screen
apps, and clicking it doesn't take focus from the app you're in. It shows:

- what Jarvis is doing (standing by, listening, thinking, speaking) and today's cost
- live captions of the conversation
- whatever Jarvis put up with `show`: markdown with images, video, PDFs and sandboxed HTML charts (links
  open in your browser), or a web page in a browser view inside the panel (you can scroll, click and type)
- the agents: Jarvis's tasks and your own Claude Code and Codex sessions, with what each last said.
  Click a row to show its answer. Hover a row to open it in Terminal or stop it.

| Hotkey (anywhere) | |
|---|---|
| ⌃⌥J | Show or hide |
| ⌃⌥↑ / ⌃⌥↓ | Opacity, which fades the glass and keeps the text readable |
| ⌃⌥K | Click-through: clicks reach the window underneath |

Drag its left or bottom edge to resize it. It stays in the corner and remembers its size and opacity.
Jarvis manages it too: it sizes the panel to fit what it shows (small, large, wide, tall), goes back to
your size afterwards, and fades or hides it when you say it's in the way. Only sizes you drag are saved.
Long answers from agents Jarvis started go up on their own, and agents are told to save charts and
screenshots as files and reference them, so those show up too. Local files are served only once
shown, only for media and HTML, and only from your home or temp folders.
jarvisd builds it from `jarvisd/panel.swift` on first run and serves the page (`jarvisd/workspace.html`)
on `127.0.0.1:8792` behind a per-run token.

## On your iPhone

Jarvis can use your iPhone's mic and whatever it plays through (like your headphones) instead of the
Mac's. Say "Hey Jarvis, show me the phone link" and scan the QR code on the panel. The page opens in
Safari: tap **Start**, then use Share › **Add to Home Screen** to keep it like an app. While it's
connected, the phone is Jarvis's mic and speaker. "Hey Jarvis" works, there's a **Talk** button, and
the panel shows an **iPhone** badge. Tap **Hand back to the Mac** or close the page to switch back.

- iOS turns a web page's mic off when Safari goes to the background or the screen locks, so the page
  keeps the screen awake while it's on.
- The page is reached only over your tailnet, via `tailscale serve --bg 8793`. Safari only gives a page
  the mic over HTTPS, so HTTPS certificates must be on in the Tailscale admin console (DNS page). The
  key in the link is in `logs/phone-key`; delete that file to change it.

## How the agents get your access

Agents can run on a different Claude Code login from your default one, so they get the claude.ai
connectors (Gmail, Google Calendar, Drive, Slack, Granola, Microsoft 365) that belong to that login's
org. `JARVIS_CLAUDE_CONFIG` points at its config folder, and defaults to `~/.claude-scorecard`. Without
it, agents use the default login. Jarvis lists your sessions from both logins and drives each one
through its own.

Agents run `claude --bg --permission-mode auto --settings jarvisd/worker-settings.json`. That loads
your normal user config, plus `enableAllProjectMcpServers` so a repo's `.mcp.json` can't stall a
background session on an approval prompt. Servers you've added with local scope to one repo (for
example the Sentry, Granola and Grafana servers in `~/Scorecard/scorecard`) only load for tasks run in
that repo.

## Cost

Measured at home: about $0.10 a minute on `gpt-realtime-2.1` and about $0.035 a minute on `-mini`. The
session only opens after the wake word and closes after 20 s of quiet. Totals are in `logs/sessions.jsonl`
and the menu bar.
