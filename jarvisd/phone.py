"""Jarvis on your iPhone: its mic and its speaker (or headphones) instead of the Mac's.

Open the phone page in Safari and it streams the phone's mic to jarvisd and plays Jarvis's voice back.
While it's connected it is Jarvis's mic and speaker (audio.Router); the panel on the Mac carries on as
usual. The page is served here on 127.0.0.1 and reached through `tailscale serve`, which gives it the
HTTPS address Safari insists on before it hands a page the mic, and keeps it inside the tailnet.
iOS stops a web page's mic when Safari goes to the background or the screen locks, so the page keeps
the screen awake while it's on.

    GET  /?k=KEY        the page. KEY lives in logs/phone-key, so a home-screen bookmark survives restarts
    WS   /ws?k=KEY      binary both ways: pcm16 24 kHz mono (mic up, Jarvis's voice down), plus JSON:
                          down  {"type": "cut"} | {"type": "status", "value"} | {"type": "lines", "value"}
                          up    {"type": "played", "samples"} (answering a cut) | {"type": "talk"}
"""
import asyncio
import json
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .audio import RATE

HERE = Path(__file__).resolve().parent
PAGE = HERE / "phone.html"
KEY_FILE = HERE.parent / "logs" / "phone-key"
PORT = 8793
LATENCY = 0.25  # s from sending audio to the phone playing it (network + its scheduling), for `busy`


def phone_key():
    try:
        return KEY_FILE.read_text().strip()
    except OSError:
        KEY_FILE.parent.mkdir(exist_ok=True)
        key = secrets.token_urlsafe(18)
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(0o600)
        return key


def tailnet_address():
    """This Mac's HTTPS name on the tailnet (what `tailscale serve` answers on), or None."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        name = json.loads(out.stdout)["Self"]["DNSName"].rstrip(".")
        return f"https://{name}" if name else None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None


def serving():
    """Whether `tailscale serve` is passing HTTPS on to the phone server."""
    try:
        out = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=5)
        return f"127.0.0.1:{PORT}" in out.stdout or f"localhost:{PORT}" in out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


class PhoneAudio:
    """The speaker half of the audio interface (add, cut, busy) for one connected phone."""

    def __init__(self, ws):
        self.ws = ws
        self.outbox = asyncio.Queue()  # one sender keeps audio and control messages in order
        self.items, self.scheduled = [], 0  # (item_id, first sample) and samples sent, since the last cut
        self.until = 0.0                    # when what's been sent should have finished playing
        self.cut_reply = None

    async def run_sender(self):
        while True:
            await self.ws.send(await self.outbox.get())

    def send_json(self, message):
        self.outbox.put_nowait(json.dumps(message))

    def add(self, item_id, pcm):
        if not self.items or self.items[-1][0] != item_id:
            self.items.append((item_id, self.scheduled))
        samples = len(pcm) // 2
        self.scheduled += samples
        self.until = max(self.until, time.monotonic() + LATENCY) + samples / RATE
        self.outbox.put_nowait(pcm)

    async def cut(self):
        """Stop playing now. Returns (item, ms of it heard), like EchoCancelledAudio.cut."""
        while not self.outbox.empty():  # audio not yet sent never needs to reach the phone
            self.outbox.get_nowait()
        self.cut_reply = asyncio.get_running_loop().create_future()
        self.send_json({"type": "cut"})
        try:
            played = await asyncio.wait_for(self.cut_reply, 0.6)
        except TimeoutError:  # no answer: estimate from what should have played by now
            played = max(0, self.scheduled - int(max(0.0, self.until - time.monotonic()) * RATE))
        played = min(played, self.scheduled)
        item, first = next(((i, f) for i, f in reversed(self.items) if f <= played), (None, 0))
        self.items, self.scheduled, self.until = [], 0, 0.0
        return item, (played - first) * 1000 // RATE

    @property
    def busy(self):
        return time.monotonic() < self.until

    def on_message(self, message):
        """JSON from the phone. Returns True for the Talk button."""
        if message.get("type") == "played" and self.cut_reply and not self.cut_reply.done():
            self.cut_reply.set_result(int(message.get("samples", 0)))
        return message.get("type") == "talk"


class PhoneServer:
    def __init__(self, router, workspace, log):
        self.router, self.workspace, self.log = router, workspace, log
        self.key = phone_key()
        self.server = None

    @property
    def link(self):
        address = tailnet_address()
        return f"{address}/?k={self.key}" if address else None

    async def start(self):
        self.server = await serve(self._connected, "127.0.0.1", PORT, process_request=self._http, max_size=None)
        return self

    def close(self):
        if self.server:
            self.server.close()

    def _http(self, connection, request):
        path, _, query = request.path.partition("?")
        if path == "/ws":
            return None if parse_qs(query).get("k", [""])[0] == self.key else connection.respond(403, "Forbidden\n")
        if path == "/":
            response = connection.respond(200, PAGE.read_text())
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            response.headers["Cache-Control"] = "no-store"
            return response
        if path == "/manifest.json":  # so "Add to Home Screen" opens it full screen, like an app
            response = connection.respond(200, json.dumps({
                "name": "Jarvis", "short_name": "Jarvis", "display": "standalone",
                "background_color": "#0e1013", "theme_color": "#0e1013", "start_url": f"/?k={self.key}"}))
            response.headers["Content-Type"] = "application/manifest+json"
            return response
        return connection.respond(404, "Not found\n")

    async def _connected(self, ws):
        phone = PhoneAudio(ws)
        replaced = self.router.attach(phone)
        if replaced:  # 4001 tells that page another one took over, so it stops instead of reconnecting
            await replaced.ws.close(code=4001, reason="another page took over")
        self.workspace.set_phone(True)
        self.log("phone connected: it's Jarvis's mic and speaker now")
        tasks = [asyncio.create_task(phone.run_sender()), asyncio.create_task(self._forward(phone))]
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self.router.from_phone(phone, message)
                elif phone.on_message(json.loads(message)):
                    self.router.talk.set()
        except (ConnectionClosed, ValueError):
            pass
        finally:
            self.log(f"phone connection closed (code {ws.close_code})")
            for task in tasks:
                task.cancel()
            self.router.detach(phone)
            if self.router.phone is None:
                self.workspace.set_phone(False)
                self.log("phone disconnected: back to the Mac's mic and speaker")

    async def _forward(self, phone):
        """Jarvis's status and the captions, for the phone page."""
        queue = self.workspace.subscribe()
        try:
            for key in ("status", "lines"):
                phone.send_json({"type": key, "value": self.workspace.state[key]})
            while True:
                key, value = await queue.get()
                if key in ("status", "lines"):
                    phone.send_json({"type": key, "value": value})
        finally:
            self.workspace.unsubscribe(queue)
