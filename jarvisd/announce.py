"""Jarvis speaking up on its own, e.g. when an agent it handed work to finishes.

Outside a conversation it speaks through OpenAI's TTS (same voice, far cheaper than opening a
Realtime session); during one, it asks the live session to pass the news on. Either way it also
posts a notification, so news isn't lost if Yash was away from the desk or on a call.
"""
import asyncio
import json
import subprocess
import urllib.request

from .audio import chime

TTS_USD_PER_MINUTE = 0.015  # gpt-4o-mini-tts, OpenAI's estimate
STYLE = "Refined, understated British accent: calm, precise, quietly dry. Measured pace, never bubbly."


class Announcer:
    def __init__(self, audio, meter, key, voice, log, ui):
        self.audio, self.meter, self.key, self.voice, self.log, self.ui = audio, meter, key, voice, log, ui
        self.loop = asyncio.get_running_loop()
        self.session = None  # the open conversation, if any

    def __call__(self, text):
        """Thread-safe: callable from the agents watcher's thread."""
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._announce(text)))

    async def _announce(self, text):
        self.log(f"announce: {text}")
        subprocess.Popen(["osascript", "-e", f"display notification {json.dumps(text, ensure_ascii=False)} "
                          'with title "Jarvis"'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.session and self.session.ws:
            await self.session.relay(text)
            return
        self.ui.line("jarvis", text)  # spoken outside a conversation: still worth a caption
        for attempt in range(3):  # the TTS call sometimes stalls outright (seen: 3 s, then a 45 s hang)
            try:
                pcm = await asyncio.to_thread(self._speech, text)
                break
            except Exception as e:
                self.log(f"announce attempt {attempt + 1} failed: {e}")
        else:
            return
        self.audio.add("announce", chime() + pcm)
        self.meter.record("announcement", len(pcm) / 48000 / 60 * TTS_USD_PER_MINUTE)

    def _speech(self, text):
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps({"model": "gpt-4o-mini-tts", "voice": self.voice, "input": text,
                             "instructions": STYLE, "response_format": "pcm"}).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:  # per read: a stall fails fast and is retried
            return r.read()  # pcm16 24 kHz mono, same as everything else
