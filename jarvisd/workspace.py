"""Jarvis's workspace: a translucent panel in the top-right corner of the screen.

It shows what's being said, the agents at work, and whatever Jarvis puts up with `show`: markdown with
images, video, PDFs and HTML charts, or a web page in a browser view. The page (workspace.html) is
served here on 127.0.0.1 and kept current over server-sent events; the panel itself is a small native
window (panel.swift) that jarvisd starts, steers over its stdin, and hears its state from on stdout.

    GET  /                          the page
    GET  /events?t=TOKEN            server-sent events: status, lines, agents, canvas, web, cost
    GET  /f/<id>/<name>             a local file put on screen (each gets its own unguessable id)
    POST /api/agent/<h>/<act>?t=..  act = show | open | stop (the agent rows' buttons)
    POST /api/canvas/clear?t=..     the canvas's close button
    POST /api/web/close?t=..        the web bar's close button

The token keeps other local programs and web pages from driving the agents through the API. Files are
served only once shown, only media and HTML types, and only from your home folder or temp folders.
"""
import asyncio
import json
import re
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
PAGE = HERE / "workspace.html"
PANEL_SRC = HERE / "panel.swift"
PANEL_BIN = HERE.parent / ".build" / "panel"
PORT = 8792
MAX_LINES = 8
MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".heic": "image/heic", ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mov": "video/quicktime", ".webm": "video/webm", ".pdf": "application/pdf",
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
}
FILE_ROOTS = [Path.home(), Path("/private/tmp"), Path("/private/var/folders")]
# ![alt](target) or [text](target) where target is a local path: /abs, ~/..., file://..., or <with spaces>
LOCAL_REF = re.compile(r"(!?)\[([^\]]*)\]\((?:<((?:file://|~|/)[^>]+)>|((?:file://|~|/)[^)\s]+))\)")


def kind_of(target):
    """image, video, pdf, html or page (anything else, e.g. a website), from the address's extension."""
    suffix = Path(urlparse(target).path if "://" in target else target).suffix.lower()
    return {".mp4": "video", ".m4v": "video", ".mov": "video", ".webm": "video", ".pdf": "pdf",
            ".html": "html", ".htm": "html"}.get(suffix, "image" if suffix in MEDIA else "page")


class Workspace:
    def __init__(self, log, port=PORT):
        self.log, self.port = log, port
        self.token = secrets.token_urlsafe(16)
        self.url = f"http://127.0.0.1:{port}/?t={self.token}"
        self.clients = set()  # one queue per connected page
        self.state = {"status": "standby", "lines": [], "agents": [], "canvas": None, "web": None, "cost": None,
                      "phone": False}
        self.files = {}        # id -> local file that was put on screen
        self.panel_state = {}  # what the panel last reported: visible, opacity, width, height, ...
        self.panel = self.server = self.poller = self.listener = None
        self.agents = self.meter = None
        self.loop = asyncio.get_running_loop()

    async def start(self, agents, meter):
        self.agents, self.meter = agents, meter
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.poller = asyncio.create_task(self._poll())
        await self._start_panel()
        return self

    async def close(self):
        for task in (self.poller, self.listener):
            if task:
                task.cancel()
        if self.panel and self.panel.returncode is None:
            self.panel.stdin.close()  # the panel exits when its input ends
        if self.server:
            self.server.close()

    # --- what the rest of jarvisd calls -------------------------------------------------------

    def status(self, status):
        """standby (waiting for the wake word), listening, thinking, speaking."""
        if status != self.state["status"]:
            self._set("status", status)

    def line(self, who, text, item=None):
        """A line of the conversation. `item` ties updates (a transcript arriving, Jarvis's words
        streaming in) to the line they belong to, so it's updated in place rather than repeated."""
        lines = self.state["lines"]
        found = next((l for l in lines if item and l["id"] == item), None)
        if found:
            found["text"] = text
        else:
            lines.append({"id": item or f"l{time.monotonic_ns()}", "who": who, "text": text, "at": time.time()})
            del lines[:-MAX_LINES]
        self._set("lines", lines)

    def extend(self, item, delta):
        """Jarvis's words as they're spoken."""
        found = next((l for l in self.state["lines"] if l["id"] == item), None)
        if found:
            found["text"] += delta
            self._set("lines", self.state["lines"])
        else:
            self.line("jarvis", delta, item)

    def drop(self, item):
        self._set("lines", [l for l in self.state["lines"] if l["id"] != item])

    def show(self, title, markdown):
        """Markdown on the canvas. Local files it refers to (images, video, PDFs, HTML) become loadable;
        returns the ones that couldn't be (missing, or not allowed)."""
        if self.state["web"]:
            self.close_web()
        markdown, missing = self._localize(markdown)
        self._set("canvas", {"title": title, "markdown": markdown, "at": time.time()})
        self.panel_command("show")
        return missing

    def show_from_thread(self, title, markdown):
        """show() for code running outside the event loop (the agents watcher)."""
        self.loop.call_soon_threadsafe(self.show, title, markdown)

    def open_web(self, url, title=None):
        """A web page in the panel's browser view, below a bar with its title and a close button."""
        self._set("web", {"url": url, "title": title or urlparse(url).netloc or url})
        self.panel_command(f"web {url}")

    def close_web(self):
        self._set("web", None)
        self.panel_command("web close")

    def clear(self):
        self._set("canvas", None)
        if self.state["web"]:
            self.close_web()

    def file_url(self, target):
        """A local file (absolute, ~, or file://) as a /f/ address the panel can load, or None if it
        doesn't exist, isn't a media or HTML file, or is outside the home and temp folders."""
        if target.startswith("file://"):
            target = unquote(urlparse(target).path)
        path = Path(target).expanduser()
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not path.is_file() or path.suffix.lower() not in MEDIA or \
                not any(path.is_relative_to(root) for root in FILE_ROOTS):
            return None
        fid = secrets.token_urlsafe(12)
        self.files[fid] = path
        return f"/f/{fid}/{quote(path.name)}"

    def _localize(self, markdown):
        missing = []

        def swap(m):
            target = m.group(3) or m.group(4)
            url = self.file_url(target)
            if not url:
                missing.append(target)
                return m.group(0)
            return f"{m.group(1)}[{m.group(2)}]({url})"
        return LOCAL_REF.sub(swap, markdown or ""), missing

    def set_phone(self, connected):
        """Whether an iPhone is Jarvis's mic and speaker right now (phone.py); the page shows a badge."""
        if connected != self.state["phone"]:
            self._set("phone", connected)

    def subscribe(self):
        """A queue of (key, value) state changes, like a page gets."""
        queue = asyncio.Queue()
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.clients.discard(queue)

    def panel_command(self, command):
        if self.panel and self.panel.returncode is None:
            self.panel.stdin.write(f"{command}\n".encode())

    async def panel_report(self, command=None):
        """Send a command (optional) and return the panel's state once it has answered."""
        before = dict(self.panel_state)
        if command:
            self.panel_command(command)
        for _ in range(10):  # it answers within a few ms, or ~0.25 s when it animates a resize
            await asyncio.sleep(0.05)
            if self.panel_state != before:
                break
        return self.panel_state or {"note": "The panel isn't running."}

    # --- keeping pages current ----------------------------------------------------------------

    def _set(self, key, value):
        self.state[key] = value
        for queue in self.clients:
            queue.put_nowait((key, value))

    async def _poll(self):
        """Agents and today's cost, refreshed while a page is watching."""
        while True:
            if self.clients:
                try:
                    sessions = await asyncio.to_thread(self.agents.sessions)
                    rows = [self._row(s) for s in sessions]
                    if rows != self.state["agents"]:
                        self._set("agents", rows)
                    cost = self.meter.totals()["today_usd"]
                    if cost != self.state["cost"]:
                        self._set("cost", cost)
                except Exception as e:
                    self.log(f"workspace poll: {e}")
            await asyncio.sleep(3)

    def _row(self, s):
        mine = self.agents.started.get(s["handle"]) or {}
        return {
            "handle": s["handle"], "agent": s["agent"], "where": s["where"], "project": s["project"],
            "name": mine.get("task") or s.get("name"), "state": s.get("state"), "said": s.get("last_said"),
            "tool": s.get("last_tool"), "started": s.get("started"), "mine": bool(mine),
        }

    # --- HTTP ---------------------------------------------------------------------------------

    async def _handle(self, reader, writer):
        try:
            head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode("latin-1")
            request, *header_lines = head.split("\r\n")
            method, target, _ = request.split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in header_lines if h)}
            path, _, query = target.partition("?")
            authorized = parse_qs(query).get("t", [""])[0] == self.token
            if method == "GET" and path == "/":
                await self._send(writer, 200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif method in ("GET", "HEAD") and path.startswith("/f/"):
                file = self.files.get(path.split("/")[2])
                if file:
                    await self._send_file(writer, file, headers.get("range"), method == "HEAD")
                else:
                    await self._send(writer, 404, b"", "text/plain")
            elif method == "GET" and path == "/events" and authorized:
                await self._events(writer)
            elif method == "POST" and path == "/api/canvas/clear" and authorized:
                self.clear()
                await self._send(writer, 200, b"{}", "application/json")
            elif method == "POST" and path == "/api/web/close" and authorized:
                self.close_web()
                await self._send(writer, 200, b"{}", "application/json")
            elif method == "POST" and path.startswith("/api/agent/") and authorized:
                _, _, _, handle, action = path.split("/", 4)
                result = await self._agent_action(unquote(handle), action)
                await self._send(writer, 200, json.dumps(result).encode(), "application/json")
            else:
                await self._send(writer, 404 if authorized or path == "/" else 403, b"", "text/plain")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError,
                ConnectionError):
            pass
        finally:
            writer.close()

    async def _send(self, writer, code, body, kind):
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}[code]
        writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
                     f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _send_file(self, writer, file, byte_range, head_only):
        """A local file, with byte ranges: WebKit won't play video from a server without them."""
        size = file.stat().st_size
        start, end, code = 0, size - 1, "200 OK"
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start, end = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
            else:  # the last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end:
                writer.write(f"HTTP/1.1 416 Range Not Satisfiable\r\nContent-Range: bytes */{size}\r\n"
                             f"Connection: close\r\n\r\n".encode())
                await writer.drain()
                return
            code = "206 Partial Content"
        extra = f"Content-Range: bytes {start}-{end}/{size}\r\n" if code.startswith("206") else ""
        writer.write(f"HTTP/1.1 {code}\r\nContent-Type: {MEDIA[file.suffix.lower()]}\r\n"
                     f"Content-Length: {end - start + 1}\r\nAccept-Ranges: bytes\r\n{extra}"
                     f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode())
        if not head_only:
            with file.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    writer.write(chunk)
                    left -= len(chunk)
                    await writer.drain()
        await writer.drain()

    async def _events(self, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n\r\n")
        queue = asyncio.Queue()
        for key, value in self.state.items():
            queue.put_nowait((key, value))
        self.clients.add(queue)
        try:
            while True:
                try:
                    key, value = await asyncio.wait_for(queue.get(), 15)
                    writer.write(f"event: {key}\ndata: {json.dumps(value)}\n\n".encode())
                except TimeoutError:
                    writer.write(b": ping\n\n")  # notices pages that went away
                await writer.drain()
        finally:
            self.clients.discard(queue)

    async def _agent_action(self, handle, action):
        if action == "show":
            title, text = await asyncio.to_thread(self.agents.raw_result, handle)
            if text is None:
                return {"error": title}
            self.show(title, text)
            return {"showing": title}
        if action in ("open", "stop"):
            return await asyncio.to_thread(getattr(self.agents, action), handle)
        return {"error": f"unknown action {action}"}

    # --- the panel ----------------------------------------------------------------------------

    async def _start_panel(self):
        try:
            if not PANEL_BIN.exists() or PANEL_BIN.stat().st_mtime < PANEL_SRC.stat().st_mtime:
                self.log("building the workspace panel (once)...")
                PANEL_BIN.parent.mkdir(exist_ok=True)
                subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", str(PANEL_BIN), str(PANEL_SRC)],
                               check=True, capture_output=True)
            self.panel = await asyncio.create_subprocess_exec(str(PANEL_BIN), self.url + "&panel=1",
                                                              stdin=asyncio.subprocess.PIPE,
                                                              stdout=asyncio.subprocess.PIPE)
            self.listener = asyncio.create_task(self._listen())
        except (subprocess.CalledProcessError, OSError) as e:
            detail = e.stderr.decode()[-300:] if getattr(e, "stderr", None) else e
            self.log(f"! workspace panel didn't start ({detail}); the page is at {self.url}")

    async def _listen(self):
        """The panel prints its state after every change (including hotkeys Yash pressed)."""
        async for line in self.panel.stdout:
            try:
                self.panel_state = json.loads(line)
            except ValueError:
                pass
