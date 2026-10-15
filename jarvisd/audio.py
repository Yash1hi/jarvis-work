"""Mic and speaker through Apple's voice processing, via the aecio helper (aecio.swift).

The OS subtracts what we play from what the mic hears, so the mic can stay open while
Jarvis talks: the wake word can't hear Jarvis, and you can interrupt it.
"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

RATE = 24000  # pcm16 mono, both directions
ROOT = Path(__file__).resolve().parent.parent
AEC_SRC = Path(__file__).with_name("aecio.swift")
AEC_BIN = ROOT / ".build" / "aecio"


def to_16k(pcm):
    """24 kHz pcm16 → 16 kHz int16 samples (what openWakeWord wants)."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return np.clip(resample_poly(x, 2, 3), -32768, 32767).astype(np.int16)


LISTENING = (880, 1320)    # rising: the wake word was heard
STOPPED = (1320, 880)      # falling: Jarvis has stopped listening


def chime(freqs=LISTENING, ms=90, volume=0.25):
    """A short two-note chime, pcm16 24 kHz: rising for "I'm listening", falling for "done"."""
    out = []
    for f in freqs:
        t = np.arange(RATE * ms // 1000) / RATE
        env = np.minimum(1, np.minimum(t, t[-1] - t) / 0.01)  # 10 ms fades
        out.append(volume * env * np.sin(2 * np.pi * f * t))
    return (np.concatenate(out) * 32767).astype("<i2").tobytes()


class EchoCancelledAudio:
    """Mic and speaker through Apple's voice processing (the aecio helper).

    The OS subtracts what we play from what the mic hears, so the mic can stay open
    while Jarvis talks. Same add/cut/busy interface as Player, plus frames() for the mic.
    """

    async def start(self):
        if not AEC_BIN.exists() or AEC_BIN.stat().st_mtime < AEC_SRC.stat().st_mtime:
            print("building the echo-cancelling audio helper (once)...")
            AEC_BIN.parent.mkdir(exist_ok=True)
            subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", str(AEC_BIN), str(AEC_SRC)], check=True)
        self.proc = await asyncio.create_subprocess_exec(
            str(AEC_BIN), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        self.mic = asyncio.Queue()
        self.sent = self.finished = 0  # buffers handed to the helper / finished playing
        self.items = []                # (item_id, first frame), counted since the last cut
        self.scheduled = 0             # frames handed over since the last cut
        self.cut_reply = None
        kind, _ = await self._read()
        if kind != b"r":
            sys.exit("echo-cancelling audio helper failed to start")
        self.pump = asyncio.create_task(self._pump())
        return self

    async def _read(self):
        header = await self.proc.stdout.readexactly(5)
        n = int.from_bytes(header[1:], "little")
        return header[:1], (await self.proc.stdout.readexactly(n) if n else b"")

    async def _pump(self):
        while True:
            kind, data = await self._read()
            if kind == b"m":
                self.mic.put_nowait((data, time.monotonic()))
            elif kind == b"d":
                self.finished = int.from_bytes(data, "little")
            elif kind == b"p" and self.cut_reply:
                self.cut_reply.set_result(int.from_bytes(data, "little"))

    def _send(self, kind, payload=b""):
        self.proc.stdin.write(kind + len(payload).to_bytes(4, "little") + payload)

    def add(self, item_id, pcm):
        if not self.items or self.items[-1][0] != item_id:
            self.items.append((item_id, self.scheduled))
        self.scheduled += len(pcm) // 2
        self.sent += 1
        self._send(b"a", pcm)

    async def cut(self):
        self.cut_reply = asyncio.get_running_loop().create_future()
        self._send(b"c")
        played = await self.cut_reply
        item, first = next(((i, f) for i, f in reversed(self.items) if f <= played), (None, 0))
        self.items, self.scheduled = [], 0
        return item, (played - first) * 1000 // RATE

    @property
    def busy(self):
        return self.sent > self.finished

    async def frames(self):
        while True:
            yield await self.mic.get()

    async def close(self):
        self.pump.cancel()
        if self.proc.returncode is None:
            self.proc.stdin.close()  # the helper exits when its input ends
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except TimeoutError:
                self.proc.kill()


class Router:
    """The mic and speaker Jarvis uses: the Mac's (EchoCancelledAudio), or a phone's while one is connected
    (phone.py). Same interface as either, so the wake word, conversations and announcements don't care.

    The Mac keeps recording while a phone is in charge (its frames are dropped), so switching back when
    the phone goes away is instant. `talk` is the phone's Talk button: a conversation without the wake word.
    """

    def __init__(self, mac):
        self.mac, self.phone = mac, None
        self.mic = asyncio.Queue()
        self.talk = asyncio.Event()
        self.pump = asyncio.create_task(self._pump_mac())

    async def _pump_mac(self):
        async for pcm, t in self.mac.frames():
            if not self.phone:
                self.mic.put_nowait((pcm, t))

    def from_phone(self, phone, pcm):
        if phone is self.phone:
            self.mic.put_nowait((pcm, time.monotonic()))

    def attach(self, phone):
        """A phone takes over; returns the one it replaced, if any (only one at a time)."""
        old, self.phone = self.phone, phone
        self._switched()
        return old

    def detach(self, phone):
        if phone is self.phone:
            self.phone = None
            self._switched()

    def _switched(self):
        while not self.mic.empty():  # audio from the old source would arrive late and out of place
            self.mic.get_nowait()
        if self.mac.busy:
            asyncio.ensure_future(self.mac.cut())

    @property
    def out(self):
        return self.phone or self.mac

    def add(self, item_id, pcm):
        self.out.add(item_id, pcm)

    async def cut(self):
        return await self.out.cut()

    @property
    def busy(self):
        return self.out.busy

    async def frames(self):
        while True:
            yield await self.mic.get()

    async def close(self):
        self.pump.cancel()
        await self.mac.close()

