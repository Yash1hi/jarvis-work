"""jarvisd: say "Hey Jarvis", talk, and hand the work to background agents.

Listens locally for the wake word; only then opens a gpt-realtime session (which
is what costs money), and closes it once the conversation goes quiet.

    uv run python -m jarvisd                # foreground
    uv run python -m jarvisd --mini         # cheaper model
"""
import argparse
import asyncio
import collections
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import certifi

# python.org's Python ships without root certificates, so HTTPS to OpenAI fails until something points
# it at a bundle. Set before any connection is made (each one builds its context from this).
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from .agents import Agents
from .announce import Announcer
from .audio import EchoCancelledAudio, Router, chime, to_16k
from .meter import Meter
from .phone import PhoneServer, serving, tailnet_address
from .session import Session
from .tools import Tools
from .wake import WakeWord
from .workspace import Workspace

ROOT = Path(__file__).resolve().parent.parent
READY = ROOT / "logs" / "ready"


def log(message):
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


KEYCHAIN_SERVICE = "jarvis-openai-api-key"


def load_key():
    """The OpenAI key: from the environment, else the login Keychain (where it should live), else .env."""
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    found = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True)
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    env = ROOT / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("OPENAI_API_KEY=") and line.split("=", 1)[1].strip().strip("'\""):
            return line.split("=", 1)[1].strip().strip("'\"")
    # (not -w with no value: its prompt silently cuts input at 128 characters, shorter than OpenAI's keys)
    sys.exit(f"OpenAI key missing. Copy it, then add it to the Keychain:\n"
             f"  security add-generic-password -U -a \"$USER\" -s {KEYCHAIN_SERVICE} -w \"$(pbpaste)\"")


def check_automation(apps):
    """Ask macOS for permission to control these apps now, at startup, rather than mid-conversation
    (the first Apple Event pops a dialog, and the call waits for Yash's answer). Apps that aren't
    running are skipped, since telling them anything would launch them."""
    for app in apps:
        if subprocess.run(["pgrep", "-xq", app]).returncode:
            continue
        try:
            out = subprocess.run(["osascript", "-e", f'tell application "{app}" to count windows'],
                                 capture_output=True, text=True, timeout=120)
            ok = out.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        log(f"control of {app}: allowed" if ok else f"! control of {app} blocked: allow it in System Settings > "
            f"Privacy & Security > Automation > Jarvis > {app}")


async def mic_is_live(audio, seconds=2.0):
    """False if the mic delivers pure digital silence (what macOS sends when access is denied)."""
    start = time.monotonic()
    async for pcm, _ in audio.frames():
        if any(pcm):
            return True
        if time.monotonic() - start > seconds:
            return False


async def wait_for_wake(audio, wake, preroll=1.5):
    """Block until "hey jarvis". Returns (score, the last `preroll` s of audio).

    The wake word fires a beat late, so the session gets the audio from just before it:
    otherwise "Hey Jarvis, what time is it" reaches the model as "time is it".
    """
    recent, last_near = collections.deque(), 0.0
    audio.talk.clear()
    async for pcm, t in audio.frames():
        if audio.talk.is_set():  # the phone's Talk button: no wake word needed
            audio.talk.clear()
            return 1.0, []
        recent.append((pcm, t))
        while t - recent[0][1] > preroll:
            recent.popleft()
        fired, score = wake.feed(to_16k(pcm))
        if fired:
            return score, list(recent)
        if score > 0.15 and t - last_near > 2:  # for tuning --threshold
            last_near = t
            log(f"near miss ({score:.2f})")


async def main(a):
    # Turning Jarvis off sends SIGTERM: unwind through the finally below (closing the audio helper and
    # browser, clearing the ready mark) instead of dying mid-step.
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    model = "gpt-realtime-2.1-mini" if a.mini else a.model
    key = load_key()
    audio = Router(await EchoCancelledAudio().start())  # the Mac's mic and speaker, or a phone's
    wake = WakeWord(a.threshold)
    meter = Meter()
    workspace = Workspace(log)
    announcer = Announcer(audio, meter, key, a.voice, log, workspace)
    agents = Agents(log, announcer, workspace.show_from_thread)
    await workspace.start(agents, meter)
    phone = await PhoneServer(audio, workspace, log).start()
    tools = Tools(meter, agents, workspace, log, phone)
    address = tailnet_address()
    log(f"phone page: {address} (ask Jarvis for the phone link)" if address and serving() else
        "phone page not reachable yet: turn on HTTPS certificates in Tailscale, then: tailscale serve --bg 8793")
    threading.Thread(target=check_automation, args=(["Terminal"],), daemon=True).start()
    if not await mic_is_live(audio):
        log("! the mic is silent: allow microphone access for this app in System Settings > "
            "Privacy & Security > Microphone, then restart jarvisd")
    log(f"ready: say \"Hey Jarvis\" ({model}, voice {a.voice})")
    READY.write_text(str(os.getpid()))  # Jarvis Controls shows "listening" from here (and checks the pid)
    try:
        while True:
            score, preroll = await wait_for_wake(audio, wake)
            log("talk button (phone)" if not preroll else f"wake word ({score:.2f})")
            audio.add("chime", chime())
            session = announcer.session = Session(audio, tools, meter, key, model, a.voice, log, workspace)
            summary = await session.run(preroll)
            if not session.connected:  # dropped while connecting (seen: "no close frame"): try once more
                log("connection to OpenAI failed; retrying")
                session = announcer.session = Session(audio, tools, meter, key, model, a.voice, log, workspace)
                summary = await session.run(preroll)
            announcer.session = None
            log(f"session: {summary['minutes']:.1f} min, ${summary['cost']:.4f} "
                f"(today ${meter.totals()['today_usd']:.2f})")
            wake.reset()
    finally:
        READY.unlink(missing_ok=True)
        phone.close()
        await workspace.close()
        await audio.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="jarvisd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="gpt-realtime-2.1")
    ap.add_argument("--mini", action="store_true", help="use gpt-realtime-2.1-mini")
    ap.add_argument("--voice", default="cedar")
    ap.add_argument("--threshold", type=float, default=0.35, help="wake word sensitivity (lower = more eager)")
    try:
        asyncio.run(main(ap.parse_args()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
