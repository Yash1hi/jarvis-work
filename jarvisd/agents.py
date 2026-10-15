"""Background agents on this Mac, driven by voice: Jarvis hands off the work and keeps talking.

Two kinds of session:
  - Yash's own, open in Terminal tabs. Jarvis reads what they last said (from Claude Code's
    transcripts) and can type a message into the tab, exactly as if he had.
  - Background tasks Jarvis starts: `claude --bg` (attach later with `claude attach`) or
    `codex exec`. With a project they run in that repo; without one they're errands, run in
    workspace/ with a brief that tells the agent it's answering a voice request.
Claude workers load Yash's normal Claude Code setup (MCP servers, plugins, skills), on his Scorecard
login so they also get its claude.ai connectors (Gmail, Calendar, Drive, Slack...). Sessions Jarvis
gives work to are watched; when one finishes, Jarvis says so.
"""
import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

HOME = Path.home()
JARVIS = Path(__file__).resolve().parent.parent
WORKSPACE = JARVIS / "workspace"  # where errands run
STARTED = JARVIS / "logs" / "started.json"  # tasks Jarvis started, so a restart doesn't lose track of them
PROJECT_ROOTS = [Path(p).expanduser() for p in os.environ.get("JARVIS_PROJECT_ROOTS", "~/Scorecard:~/Code").split(":")]
# Claude Code logins (config folders). Agents run on the work login, because the claude.ai connectors
# (Gmail, Calendar, Drive, Slack, Granola...) belong to that login's org, not the default one's. Yash's own
# sessions can be on either login, so both are listed, and each session is driven through its own.
DEFAULT_CONFIG = HOME / ".claude"
AGENT_CONFIG = Path(os.environ.get("JARVIS_CLAUDE_CONFIG", HOME / ".claude-scorecard")).expanduser()
if not AGENT_CONFIG.is_dir():
    AGENT_CONFIG = DEFAULT_CONFIG
CONFIGS = list(dict.fromkeys([AGENT_CONFIG, DEFAULT_CONFIG]))
CLAUDE = shutil.which("claude") or str(HOME / ".local" / "bin" / "claude")
CODEX = shutil.which("codex") or next(iter(sorted(glob.glob(str(HOME / ".nvm/versions/node/*/bin/codex")))), "codex")
# Loaded on top of Yash's own settings. A repo's .mcp.json would otherwise stop a background session
# on an approval prompt nobody can answer.
WORKER_SETTINGS = Path(__file__).with_name("worker-settings.json")
TASK_BRIEF = """You're a background worker for Jarvis, Yash's voice assistant on his work Mac. You have his
normal Claude Code setup (MCP servers, plugins, skills and CLIs), so use whichever of them the task needs.

Task (spoken aloud, so read it generously): {task}

Don't send email or messages, post, publish, merge, deploy, delete, spend money or change settings unless
the task asks for exactly that. If it needs one of those, stop and say what's left for Yash to do.
Jarvis reads the start of your final message aloud: open it with one plain sentence that answers or sums
up, with no markdown. If you need a decision from Yash, end with one clear question instead. Detail can
follow; it goes up on his screen. {media}"""
MEDIA_NOTE = ("When a picture says it better (a chart, a screenshot, a diagram), save it as a PNG or SVG, or "
              "a self-contained HTML file for an interactive chart, in your working folder, and put it in your "
              "final message as ![caption](/absolute/path); it's shown on his screen.")
# Added to repo tasks, which otherwise go through as Yash said them.
VOICE_NOTE = """

(This came by voice through Jarvis, who reads the start of your final message aloud and puts the rest on
Yash's screen. Open it with one plain sentence that answers or sums up the outcome, not a lead-in like
"Here's what I found". If you need a decision, end with one clear question. {media})"""
TASK_BRIEF = TASK_BRIEF.replace("{media}", MEDIA_NOTE)
VOICE_NOTE = VOICE_NOTE.format(media=MEDIA_NOTE)
LONG_ANSWER = 280  # characters: longer final messages go up on the workspace panel by themselves


# Without Claude Code's own session variables: started from inside a Claude Code session, jarvisd inherits
# them, and agents would take themselves for that session's children. The login is set per command.
AGENT_ENV = {k: v for k, v in os.environ.items() if not (k in ("CLAUDECODE", "CLAUDE_CONFIG_DIR")
             or k.startswith(("CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT")))}


def env_for(config):
    """The environment for a command on one Claude Code login (the default needs no CLAUDE_CONFIG_DIR)."""
    return AGENT_ENV if config == DEFAULT_CONFIG else {**AGENT_ENV, "CLAUDE_CONFIG_DIR": str(config)}


def _run(cmd, cwd=None, timeout=30, config=DEFAULT_CONFIG):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env_for(config))


def projects():
    """Folders a task can run in: the project roots themselves, every folder in them, and Jarvis."""
    dirs = {}
    for root in PROJECT_ROOTS:
        if not root.is_dir():
            continue
        dirs.setdefault(root.name, root)
        for p in root.iterdir():
            if p.is_dir() and not p.name.startswith((".", "__")):
                dirs.setdefault(p.name, p)
    dirs.setdefault(JARVIS.name, JARVIS)
    return dirs


def find_project(name):
    dirs = projects()
    by_lower = {k.lower(): v for k, v in dirs.items()}
    if name.lower() in by_lower:
        return by_lower[name.lower()]
    close = difflib.get_close_matches(name.lower(), list(by_lower), n=1, cutoff=0.6)
    return by_lower[close[0]] if close else None


def _plain(text, limit):
    """Agent output as speakable text: no markdown, no code blocks, trimmed."""
    text = re.sub(r"```.*?```", " (code) ", text, flags=re.S)
    text = re.sub(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*$", " ", text, flags=re.M)  # table rules and ---
    text = re.sub(r"[`*_#>|]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def headline(text):
    """What to say when a task finishes: its first real sentence (not a lead-in like "Here's what I
    found."), or its closing question if it asks one."""
    sentences = re.split(r"(?<=[.!?:])\s", _plain(text or "", 4000))
    if sentences[-1].endswith("?"):
        return sentences[-1][:200]
    lead_in = re.compile(r"^(here'?s|here is|here are|below|done)\b.{0,60}[.:!]$|.*:$", re.I)
    return next((x for x in sentences if not lead_in.match(x)), sentences[0])[:200]


def claude_last_said(session_id, cwd, config=DEFAULT_CONFIG):
    """The last thing a Claude Code session said, and the last tool it reached for."""
    projects_dir = config / "projects"
    path = projects_dir / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"
    if not path.exists():
        found = list(projects_dir.glob(f"*/{session_id}.jsonl"))
        if not found:
            return None, None
        path = found[0]
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 400_000))
        lines = f.read().decode(errors="ignore").splitlines()
    said = doing = None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        for block in reversed(e.get("message", {}).get("content", [])):
            if not isinstance(block, dict):
                continue
            if doing is None and block.get("type") == "tool_use":
                doing = block.get("name")
            if said is None and block.get("type") == "text" and block.get("text", "").strip():
                said = block["text"]
        if said:
            break
    return said, doing


def _terminal_tab_script(tty, body):
    return f'''tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "{tty}" then
        {body}
        return "ok"
      end if
    end repeat
  end repeat
  return "missing"
end tell'''


def _tty_of(pid):
    tty = _run(["ps", "-o", "tty=", "-p", str(pid)]).stdout.strip()
    return f"/dev/{tty}" if tty and tty != "??" else None


TERMINAL_AUTOMATION = ("macOS isn't letting Jarvis control Terminal yet. Allow it in System Settings > Privacy & "
                       "Security > Automation > Jarvis > Terminal (or answer the dialog asking).")


def _osascript(script):
    """Run AppleScript against Terminal; fail fast (with the reason) when macOS hasn't allowed it."""
    try:
        out = _run(["osascript", "-e", script], timeout=6)
    except subprocess.TimeoutExpired:
        raise RuntimeError(TERMINAL_AUTOMATION) from None
    if "-1743" in out.stderr or "Not authorized" in out.stderr:
        raise RuntimeError(TERMINAL_AUTOMATION)
    return out.stdout.strip()


def type_into_terminal(pid, message):
    """Type `message` + Return into the Terminal tab running `pid`, as if Yash typed it."""
    tty = _tty_of(pid)
    if not tty:
        return False
    text = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return _osascript(_terminal_tab_script(tty, f'do script "{text}" in t')) == "ok"


def show_terminal(pid):
    tty = _tty_of(pid)
    if not tty:
        return False
    body = "set selected of t to true\n        set index of w to 1\n        activate"
    return _osascript(_terminal_tab_script(tty, body)) == "ok"


def open_in_new_terminal(command):
    script = f'tell application "Terminal"\n  do script "{command}"\n  activate\nend tell'
    _osascript(script)


class CodexJob:
    """A `codex exec` task Jarvis started, followed through its JSON event stream."""

    def __init__(self, cwd, task):
        self.cwd, self.task = Path(cwd), task
        self.thread_id, self.last_said, self.error = None, None, None
        self.state, self.at = "working", time.time()
        self._start([CODEX, "exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", task])

    def _start(self, cmd):
        self.state = "working"
        self.proc = subprocess.Popen(cmd, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, text=True, env=AGENT_ENV)
        threading.Thread(target=self._follow, daemon=True).start()

    def _follow(self):
        for line in self.proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "thread.started":
                self.thread_id = e.get("thread_id")
            item = e.get("item") or {}
            if e.get("type") == "item.completed" and item.get("type") == "agent_message":
                self.last_said = item.get("text")
            if e.get("type") in ("turn.failed", "error"):
                self.error = json.dumps(e.get("error") or e)[:300]
        self.proc.wait()
        self.state = "failed" if self.proc.returncode else "done"

    def follow_up(self, message):
        if self.state == "working":
            return False
        self._start([CODEX, "exec", "resume", "--json", "--skip-git-repo-check", self.thread_id, message])
        return True

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()


class Agents:
    def __init__(self, log, announce, show=None):
        """`show(title, markdown)` puts a finished task's long answer on the workspace (thread-safe)."""
        self.log, self.announce, self.show = log, announce, show
        self.codex_jobs = {}  # handle -> CodexJob
        self.watched = {}     # session key -> (label, last state)
        self.started = {}     # handle -> {"task", "at", "label", "watch"} for the tasks Jarvis started
        self._lock = threading.Lock()
        WORKSPACE.mkdir(exist_ok=True)
        self._load_started()
        threading.Thread(target=self._watch, daemon=True).start()

    def _load_started(self):
        """Pick up after a restart: tasks from the last day, still watched if never announced. A Claude
        task that finished while jarvisd was down is announced as soon as the watcher sees it."""
        try:
            saved = json.loads(STARTED.read_text())
        except (OSError, ValueError):
            return
        self.started = {h: info for h, info in saved.items() if time.time() - info.get("at", 0) < 86400}
        for handle, info in self.started.items():
            if info.get("watch") and info.get("agent") == "claude":  # codex jobs die with jarvisd
                self.watched[("claude", handle)] = (info["label"], "queued")

    def _remember(self, handle, **info):
        with self._lock:
            self.started.setdefault(handle, {}).update(info)
            STARTED.parent.mkdir(exist_ok=True)
            STARTED.write_text(json.dumps(self.started))

    # --- what's running -------------------------------------------------------------

    def sessions(self):
        out = []
        claude = []
        for config in CONFIGS:  # each login keeps its own sessions
            try:
                listed = json.loads(_run([CLAUDE, "agents", "--json", "--all"], config=config).stdout or "[]")
            except (ValueError, subprocess.TimeoutExpired):
                listed = []
            claude += [(s, config) for s in listed]
        recent = time.time() * 1000 - 12 * 3600 * 1000
        for s, config in claude:
            background = s.get("kind") == "background"
            if background and s.get("startedAt", 0) < recent and s.get("state") != "working":
                continue  # old, finished background sessions
            said, doing = claude_last_said(s["sessionId"], s["cwd"], config)
            out.append({
                "handle": s.get("id") or s.get("name"),
                "agent": "claude",
                "where": "background" if background else "Terminal tab",
                "project": _project_name(s["cwd"]),
                "name": s.get("name"),
                "state": s.get("state") or s.get("status"),
                "last_said": _plain(said, 300) if said else None,
                "last_tool": doing,
                "started": s.get("startedAt", 0) / 1000 or None,
                "_session": s,
                "_config": config,
            })
        for handle, job in self.codex_jobs.items():
            out.append({"handle": handle, "agent": "codex", "where": "background", "project": _project_name(job.cwd),
                        "name": job.task[:60], "state": job.state, "error": job.error, "started": job.at,
                        "last_said": _plain(job.last_said, 300) if job.last_said else None})
        for pid, cwd in self._interactive_codex():
            out.append({"handle": f"codex-{pid}", "agent": "codex", "where": "Terminal tab",
                        "project": _project_name(cwd), "name": None, "state": "open", "_pid": pid})
        return out

    def _interactive_codex(self):
        found = []
        for line in _run(["ps", "-A", "-o", "pid=,tty=,command="]).stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] != "??" and re.match(r"(\S*/)?codex(\s|$)", parts[2]) \
                    and " exec" not in parts[2]:
                cwd = _run(["lsof", "-a", "-p", parts[0], "-d", "cwd", "-Fn"]).stdout
                m = re.search(r"^n(.+)$", cwd, re.M)
                found.append((int(parts[0]), m.group(1) if m else "?"))
        return found

    def resolve(self, target):
        """A session from a spoken reference: its handle, its name, or its project (if unique)."""
        sessions = self.sessions()
        t = target.lower().strip()
        for s in sessions:
            if t in ((s["handle"] or "").lower(), (s["name"] or "").lower()):
                return s, None
        matches = [s for s in sessions if s["project"].lower() == t] or \
                  [s for s in sessions if t in s["project"].lower() or t in (s["name"] or "").lower()]
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, f"No session matches '{target}'."
        return None, {"ambiguous": [{k: s[k] for k in ("handle", "agent", "project", "name", "last_said")}
                                    for s in matches]}

    # --- doing things -------------------------------------------------------------------

    def start(self, agent, task, project=None):
        """A repo task when `project` is given, otherwise an errand (with the brief, in workspace/)."""
        if project:
            cwd = find_project(project)
            if not cwd:
                return {"error": f"No project folder matches '{project}'.", "projects": sorted(projects())}
            prompt, label = task + VOICE_NOTE, f"{agent.title()} in {cwd.name}"
        else:
            cwd = WORKSPACE
            prompt = task if agent == "codex" else TASK_BRIEF.format(task=task)
            label = f"The background task ({_plain(task, 60)})"
        if agent == "codex":
            handle = f"codex-{len(self.codex_jobs) + 1}"
            self.codex_jobs[handle] = CodexJob(cwd, prompt)
            self._remember(handle, task=task, at=time.time(), label=label, agent="codex", watch=True)
            self._watch_session(("codex", handle), label)
            return {"started": handle, "agent": "codex", "project": _project_name(cwd)}
        # "--": a prompt starting with "-" would otherwise be read as an option
        out = _run([CLAUDE, "--bg", "--permission-mode", "auto", "--settings", str(WORKER_SETTINGS), "--", prompt],
                   cwd=cwd, config=AGENT_CONFIG)
        m = re.search(r"backgrounded · (\w+)", out.stdout)
        if not m:
            return {"error": (out.stdout + out.stderr).strip()[:300]}
        self._remember(m.group(1), task=task, at=time.time(), label=label, agent="claude", watch=True)
        self._watch_session(("claude", m.group(1)), label)
        return {"started": m.group(1), "agent": "claude", "project": _project_name(cwd),
                "note": "Runs in the background; Jarvis announces when it's done."}

    def result(self, target):
        """Everything a session last said, for when Yash wants more than the announcement."""
        session, problem = self.resolve(target)
        if problem:
            return {"error": problem} if isinstance(problem, str) else problem
        said = self._full_text(session)
        if said is None and session["where"] == "Terminal tab" and session["agent"] == "codex":
            return {"error": "I can't read Codex sessions open in Terminal; agent_open puts it on screen."}
        return {"handle": session["handle"], "state": session["state"],
                "said": _plain(said, 3000) if said else "(nothing yet)"}

    def raw_result(self, target):
        """(title, markdown) of a session's last message for the workspace, or (error, None)."""
        session, problem = self.resolve(target)
        if problem:
            return (problem if isinstance(problem, str) else f"'{target}' matches more than one session."), None
        said = self._full_text(session)
        if not said:
            return "That session hasn't said anything yet.", None
        task = (self.started.get(session["handle"]) or {}).get("task")
        return (_plain(task, 80) if task else session["name"] or session["project"]), said

    def send(self, target, message, confirmed=False):
        session, problem = self.resolve(target)
        if problem:
            return {"error": problem} if isinstance(problem, str) else problem
        label = f"{session['agent'].title()} in {session['project']}"
        if session["where"] == "Terminal tab" and not confirmed:
            # Typing into Yash's own session is as good as him typing it, and speech gets misheard.
            return {"not_sent_yet": True, "ask_yash": f"Send \"{message}\" to {label} ({session['handle']})?",
                    "then": "Call agent_send again with confirmed=true and the same session once he says yes."}
        if session["agent"] == "codex" and session["where"] == "background":
            job = self.codex_jobs[session["handle"]]
            if not job.follow_up(message):
                return {"error": "That Codex task is still working; wait for it to finish."}
            self._watch_session(("codex", session["handle"]), label)
        elif session["where"] == "Terminal tab":
            pid = session.get("_pid") or session["_session"]["pid"]
            if not type_into_terminal(pid, message):
                return {"error": "That session isn't in a Terminal tab I can type into."}
            if session["agent"] == "claude":
                self._watch_session(("claude", session["_session"]["sessionId"]), label)
        else:  # a background Claude session: stop it, then wake it with the new message
            s, config = session["_session"], session["_config"]
            _run([CLAUDE, "stop", s["id"]], config=config)
            # no flags here: passing any makes --resume fork a copy instead of continuing
            out = _run([CLAUDE, "--bg", "--resume", s["sessionId"], message], cwd=s["cwd"], config=config)
            if out.returncode:
                return {"error": (out.stdout + out.stderr).strip()[:300]}
            self._watch_session(("claude", s["id"]), label)
        self.log(f"agents: sent to {label}: {message}")
        return {"sent": True, "to": label, "where": session["where"]}

    def open(self, target):
        session, problem = self.resolve(target)
        if problem:
            return {"error": problem} if isinstance(problem, str) else problem
        if session["where"] == "Terminal tab":
            ok = show_terminal(session.get("_pid") or session["_session"]["pid"])
            return {"opened": ok}
        if session["agent"] == "claude":
            s, config = session["_session"], session["_config"]
            login = "" if config == DEFAULT_CONFIG else f"CLAUDE_CONFIG_DIR='{config}' "
            open_in_new_terminal(f"cd '{s['cwd']}' && {login}{CLAUDE} attach {s['id']}")
            return {"opened": True, "note": "Opened in a new Terminal window."}
        job = self.codex_jobs[session["handle"]]
        if not job.thread_id or job.state == "working":
            return {"error": "That Codex task is still running; it can be opened once it finishes."}
        open_in_new_terminal(f"cd '{job.cwd}' && {CODEX} resume {job.thread_id}")
        return {"opened": True}

    def stop(self, target):
        session, problem = self.resolve(target)
        if problem:
            return {"error": problem} if isinstance(problem, str) else problem
        if session["where"] == "Terminal tab":
            return {"error": "That's one of Yash's own Terminal sessions; I won't stop it."}
        if session["agent"] == "claude":
            _run([CLAUDE, "stop", session["_session"]["id"]], config=session["_config"])
        else:
            self.codex_jobs[session["handle"]].stop()
        return {"stopped": True}

    # --- noticing when work finishes ---------------------------------------------------------

    def _watch_session(self, key, label):
        # "queued" until the watcher has seen it: right after typing into a tab, Claude can still
        # read idle for a moment, which must not count as finished.
        self.watched[key] = (label, "queued")
        if key[1] in self.started:  # e.g. a follow-up sent to a task Jarvis started: watch it across restarts too
            self._remember(key[1], watch=True)

    def _watch(self):
        while True:
            time.sleep(3)
            if not self.watched:
                continue
            try:
                current = {}
                for s in self.sessions():
                    if s["agent"] == "claude":
                        current[("claude", s["handle"])] = s
                        current[("claude", s["_session"]["sessionId"])] = s
                    else:
                        current[("codex", s["handle"])] = s
                for key, (label, before) in list(self.watched.items()):
                    s = current.get(key)
                    if not s:
                        continue
                    now = s["state"]
                    # "blocked": a background session that ended its turn waiting on Yash (a question
                    # or a permission it can't take on its own)
                    finished = (before in ("working", "busy") and now in ("done", "idle", "failed", "blocked")) or \
                               (before == "queued" and now in ("done", "failed", "blocked"))  # done before we looked
                    if finished:
                        del self.watched[key]
                        if key[1] in self.started:
                            self._remember(key[1], watch=False)
                        said = self._full_text(s)
                        verb = {"failed": "ran into a problem", "blocked": "is waiting for you"}.get(now, "has finished")
                        line = headline(said)
                        shown = ""
                        task = (self.started.get(key[1]) or {}).get("task")
                        if task and self.show and said and len(said) > LONG_ANSWER:  # a task Jarvis started
                            self.show(_plain(task, 80), said)
                            shown = " The full answer is on your panel."
                        self.announce(f"{label} {verb}." + (f" It says: {line}" if line else "") + shown)
                    else:
                        self.watched[key] = (label, now)
            except Exception as e:  # never let the watcher die
                self.log(f"agents watcher: {e}")

    def _full_text(self, s):
        """The whole final message (sessions() trims it), so a closing question isn't cut off."""
        if s["agent"] == "claude":
            return claude_last_said(s["_session"]["sessionId"], s["_session"]["cwd"], s["_config"])[0]
        job = self.codex_jobs.get(s["handle"])
        return job.last_said if job else None


def _project_name(cwd):
    return "errands" if Path(cwd) == WORKSPACE else Path(cwd).name


def public(session):
    """A session as the model sees it (no internal fields)."""
    return {k: v for k, v in session.items() if not k.startswith("_") and v is not None}
