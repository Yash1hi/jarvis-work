"""One conversation: a gpt-realtime session from the wake word until it goes quiet."""
import asyncio
import base64
import difflib
import json
import re
import time
from datetime import datetime

import websockets

from .audio import RATE, STOPPED, chime
from .tools import SCHEMAS

# USD per 1M tokens: text in, cached text in, text out, audio in, cached audio in, audio out.
# Flagship audio rates from OpenAI's pricing page; mini rates from third parties (checked 2026-10-13).
PRICES = {
    "gpt-realtime-2.1": (4.00, 0.40, 16.00, 32.00, 0.40, 64.00),
    "gpt-realtime-2.1-mini": (0.60, 0.06, 2.40, 10.00, 0.30, 20.00),
}

INSTRUCTIONS = """You are JARVIS, Yash's assistant on his work Mac at Scorecard. Speak with a refined,
understated British accent: calm, precise, quietly dry. Measured pace, never bubbly or salesy.
Keep spoken replies to one or two sentences.
- Yash wakes you with "Hey Jarvis", and a chime has already answered that. Never greet him or respond to
  the wake phrase; just respond to what he asks.
- You don't do the work yourself; background agents do, and you keep the conversation going meanwhile.
  agent_start hands one a task and returns at once. Acknowledge it once, in a few words ("On it."),
  either before the call or after it, never both, and don't add that results will follow.
  Claude agents have Yash's whole work setup, so anything that needs his tools, accounts, repos or the
  web goes to one. Without a project it's an errand; with one it works in that repo. Start several if
  he asks for several things.
- Answer from your own knowledge only for quick general questions. Never guess at his data (runs,
  tickets, messages, calendar, code): send an agent.
- Your workspace is a translucent panel in the top-right corner of Yash's screen, and it's yours to run.
  It always shows your agents and what's being said. Anything with detail (lists, numbers, tables, a
  draft, an agent's full answer) goes there with show, then just say it's on screen; don't read long
  text aloud. show also takes images, video, PDFs and charts (![caption](address) in markdown, or url
  for one thing on its own), and opens web pages in a browser view in the panel (url).
- Fit the panel to what's on it without being asked: pass size with show (large for web pages, images
  and long reads, wide for wide tables, small when there's little on it), and go back to reset when
  you've finished with something big. If Yash says it's in the way, fade it (workspace opacity, or
  change -0.3) or hide it; when you put up something he needs to read, make sure it's visible and
  solid enough to read. Every show and workspace result tells you the panel's current state. If he
  can't click or scroll the panel, click-through is probably on (⌃⌥K toggles it): turn it off.
- When news about an agent arrives, pass it on in a sentence. A long answer from a task you started is
  already on the panel by then; don't show it again. If the agent asked a question, put it to Yash and
  send his answer back with agent_send. agent_result reads you its full answer, for when you
  need to talk about it rather than show it.
- agents lists what's running. agent_send gives a running agent a follow-up; for his own Terminal
  sessions it first hands back the message to read back, and sends only once he says yes.
- spending says what Jarvis itself has cost in OpenAI fees.
- Yash can use Jarvis from his iPhone, with its mic and his headphones: phone_link puts a QR code for it
  on the panel. While the phone is connected, it's your mic and speaker.
- Never close with offers like "anything else?" or "I can help with...": when you've answered, stop.
- Say plainly what tools actually did. If a result has an error, tell Yash; never claim an agent did
  something its result doesn't say.
- When Yash is done ("go away", "that's all", "thanks", "stop listening"), call end_conversation and say
  nothing at all: a chime tells him you've stopped listening."""


# Apple's echo canceller partly forgets the room after ~45 s of quiet and takes ~2.5 s of
# playback to re-adapt, so Jarvis's first words after a pause can leak into the mic. Speech
# detected in that window is treated as Jarvis's own echo, not an interruption.
ECHO_GRACE = 2.5

# Sounds jarvisd plays itself (audio.add ids), which the model never produced and can't truncate.
LOCAL_SOUNDS = {"chime", "stopped", "announce"}

# Tools that need no spoken follow-up when Jarvis already spoke in the same response and they went fine.
QUIET_AFTER = {"agent_start", "agent_send", "agent_stop", "show", "workspace"}


def needs_reply(result):
    """A tool result Jarvis has to say something about: an error, a question, a choice."""
    return any(k in result for k in ("error", "ask_yash", "ambiguous", "not_shown", "queued"))
QUIET_BEFORE_GRACE = 10


WAKE_FILLER = {"hey", "hi", "hello", "ok", "okay", "yo", "uh", "um", "so", "oh"}


def wake_only(transcript):
    """True if all Yash said was the wake phrase ("Uh, hey Jarvis."), allowing for mishearings."""
    words = re.findall(r"[a-z]+", transcript.lower())
    rest = [w for w in words if w not in WAKE_FILLER and difflib.SequenceMatcher(None, w, "jarvis").ratio() < 0.6]
    return not rest  # an empty transcript (noise) counts too: nothing to answer


def response_cost(model, usage):
    p = PRICES.get(model)
    if not p or not usage:
        return 0.0
    i, o = usage.get("input_token_details", {}), usage.get("output_token_details", {})
    cached = i.get("cached_tokens_details", {})
    ct, ca = cached.get("text_tokens", 0), cached.get("audio_tokens", 0)
    return (
        (i.get("text_tokens", 0) - ct) * p[0] + ct * p[1] + o.get("text_tokens", 0) * p[2]
        + (i.get("audio_tokens", 0) - ca) * p[3] + ca * p[4] + o.get("audio_tokens", 0) * p[5]
    ) / 1e6


class Session:
    def __init__(self, audio, tools, meter, key, model, voice, log, ui, first_idle=10, idle=20):
        self.audio, self.tools, self.meter, self.key = audio, tools, meter, key
        self.model, self.voice, self.log = model, voice, log
        self.ui = ui  # the workspace panel: status and captions
        self.first_idle, self.idle = first_idle, idle

    def _config(self):
        return {
            "type": "realtime",
            "model": self.model,
            "instructions": INSTRUCTIONS + f"\nIt's {datetime.now():%A %-d %B, %-I:%M %p} here.",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": RATE},
                    "noise_reduction": {"type": "far_field"},
                    "transcription": {"model": "gpt-4o-mini-transcribe", "language": "en"},  # else it guesses (saw 对。)
                    # We decide about interruptions and replies ourselves (see _receive), so echo
                    # leaking past the canceller can't cut Jarvis off or become a fake turn.
                    "turn_detection": {"type": "server_vad", "silence_duration_ms": 500, "threshold": 0.6,
                                       "create_response": False, "interrupt_response": False},
                },
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.voice},
            },
            "tools": SCHEMAS,
            "tool_choice": "auto",
        }

    async def run(self, preroll=()):
        """Talk until the conversation ends or goes idle. Returns a summary for the session log.

        `preroll` is the audio from just before the wake word fired, sent first.
        """
        self.started = self.activity = time.monotonic()
        self.heard_speech, self.responding = False, False
        self.meter.start(self.model)
        self.last_playing = float("-inf")  # last time Jarvis's audio was playing
        self.grace_until = 0.0             # speech before this is probably Jarvis's own echo
        self.echo_turn = False             # the turn in progress started inside the grace window
        self.echo_items = set()
        self.tools.end_requested = False
        self.ws, self.pending_relay, self.want_response = None, False, False
        self.first_turn, self.held_turn = True, None  # see _hold_first_turn
        self.connected = False  # any event from OpenAI yet; if not, __main__ retries the session
        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        self.ui.status("listening")
        try:
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {self.key}"},
                                          max_size=None) as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "session.update", "session": self._config()}))
                tasks = [asyncio.create_task(self._send(ws, preroll)), asyncio.create_task(self._watch(ws))]
                try:
                    await self._receive(ws)
                finally:
                    for task in tasks:
                        task.cancel()
        except websockets.ConnectionClosedOK:
            pass
        except (websockets.WebSocketException, OSError) as e:
            self.log(f"session error: {e}")
        self.ws = None
        self.ui.status("standby")
        return self.meter.finish()

    async def relay(self, text):
        """News from outside the conversation (an agent finished): have Jarvis pass it on."""
        await self.ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system",
            "content": [{"type": "input_text", "text": f"Tell Yash, briefly and in your own words: {text}"}]}}))
        if self.responding:
            self.pending_relay = True  # after the current reply
        else:
            await self.ws.send(json.dumps({"type": "response.create"}))

    async def _send(self, ws, preroll):
        for pcm, _ in preroll:
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))
        async for pcm, _ in self.audio.frames():
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))

    async def _watch(self, ws):
        """Close when Yash said goodbye, or nothing has happened for a while (that's what caps cost)."""
        while True:
            await asyncio.sleep(0.5)
            self.ui.status("speaking" if self.audio.busy else "thinking" if self.responding else "listening")
            if self.audio.busy:
                self.last_playing = time.monotonic()
            if self.audio.busy or self.responding:
                self.activity = time.monotonic()
                continue
            if self.tools.end_requested:
                self.log("conversation ended")
                await ws.close()
                return
            limit = self.idle if self.heard_speech else self.first_idle
            if time.monotonic() - self.activity > limit:
                self.log(f"quiet for {limit}s: closing the session")
                await ws.close()
                return

    async def _receive(self, ws):
        turn_end = None
        async for raw in ws:
            self.connected = True
            ev = json.loads(raw)
            kind = ev["type"]
            if kind == "error":
                if ev["error"].get("code") == "conversation_already_has_active_response":
                    self.want_response = True  # Yash spoke as a reply was starting: answer once it's done
                else:
                    self.log(f"! {ev['error'].get('message')} ({ev['error'].get('code')})")
            elif kind == "input_audio_buffer.speech_started":
                self.activity = time.monotonic()
                self.echo_turn = self.audio.busy and time.monotonic() < self.grace_until
                if not self.echo_turn:
                    self.heard_speech = True
                    await self._interrupt(ws)
            elif kind == "input_audio_buffer.speech_stopped":
                turn_end = None if self.echo_turn else time.monotonic()
            elif kind == "input_audio_buffer.committed":
                if self.echo_turn:  # drop it, so the model never "hears" its own voice
                    self.echo_items.add(ev["item_id"])
                    await ws.send(json.dumps({"type": "conversation.item.delete", "item_id": ev["item_id"]}))
                elif self.first_turn:
                    self.first_turn = False
                    self.ui.line("you", "", ev["item_id"])  # a placeholder, so it stays above the answer
                    await self._hold_first_turn(ws, ev["item_id"])
                else:
                    self.ui.line("you", "", ev["item_id"])
                    await self._reply(ws)
                self.echo_turn = False
            elif kind == "conversation.item.input_audio_transcription.completed":
                if ev["item_id"] == self.held_turn:
                    await self._release_first_turn(ws, ev["transcript"])  # logs it either way
                elif ev["item_id"] in self.echo_items:
                    self.log(f"  (ignored echo: {ev['transcript'].strip()})")
                else:
                    self.log(f"you:    {ev['transcript'].strip()}")
                    self.ui.line("you", ev["transcript"].strip(), ev["item_id"])
            elif kind == "response.created":
                self.responding = True
            elif kind == "response.output_item.added" and ev["item"].get("name") == "end_conversation":
                await self.audio.cut()  # no goodbye, even if the model started one: the chime says it
            elif kind == "response.output_audio.delta":
                if not self.audio.busy and time.monotonic() - self.last_playing > QUIET_BEFORE_GRACE:
                    self.grace_until = time.monotonic() + ECHO_GRACE
                if turn_end:
                    self.log(f"  ⏱ {time.monotonic() - turn_end:.2f}s to first audio")
                    turn_end = None
                self.audio.add(ev["item_id"], base64.b64decode(ev["delta"]))
            elif kind == "response.output_audio_transcript.delta":
                self.ui.extend(ev["item_id"], ev["delta"])
            elif kind == "response.output_audio_transcript.done":
                self.log(f"jarvis: {ev['transcript'].strip()}")
                self.ui.line("jarvis", ev["transcript"].strip(), ev["item_id"])
            elif kind == "response.done":
                await self._response_done(ws, ev["response"])

    async def _reply(self, ws):
        if self.responding:
            self.want_response = True  # the last reply is still finishing (or cancelling): wait for it
        else:
            await ws.send(json.dumps({"type": "response.create"}))

    async def _hold_first_turn(self, ws, item_id):
        """The first turn includes the audio from before the wake word fired, so it's often just
        "Hey Jarvis", which the model would answer with a greeting. Wait for the transcript, and drop
        the turn if that's all it was (the chime already acknowledged him)."""
        self.held_turn = item_id

        async def give_up_waiting():  # if the transcript never comes, answer anyway
            await asyncio.sleep(2.5)
            if self.held_turn == item_id:
                self.held_turn = None
                await self._reply(ws)
        asyncio.create_task(give_up_waiting())

    async def _release_first_turn(self, ws, transcript):
        item_id, self.held_turn = self.held_turn, None
        if wake_only(transcript):
            self.echo_items.add(item_id)  # logged as ignored, not as something he said
            await ws.send(json.dumps({"type": "conversation.item.delete", "item_id": item_id}))
            self.log(f"(just the wake word: {transcript.strip()})")
            self.ui.drop(item_id)
        else:
            self.log(f"you:    {transcript.strip()}")
            self.ui.line("you", transcript.strip(), item_id)
            await self._reply(ws)

    async def _interrupt(self, ws):
        """Yash started talking: stop Jarvis mid-sentence and tell the model how much was heard."""
        if self.responding:
            await ws.send(json.dumps({"type": "response.cancel"}))
        if self.audio.busy:
            item, ms = await self.audio.cut()
            if item and item not in LOCAL_SOUNDS:  # the chime and announcements aren't in the conversation
                await ws.send(json.dumps({"type": "conversation.item.truncate", "item_id": item,
                                          "content_index": 0, "audio_end_ms": ms}))
                self.log(f"(interrupted after {ms / 1000:.1f}s)")

    @staticmethod
    def _spoke(resp):
        """Whether the response said something out loud alongside its tool calls."""
        return any(o.get("type") == "message" and any(c.get("transcript") for c in o.get("content", []))
                   for o in resp.get("output", []))

    async def _response_done(self, ws, resp):
        self.meter.add(response_cost(self.model, resp.get("usage")))
        if resp["status"] not in ("completed", "cancelled"):
            self.log(f"! response {resp['status']}: {json.dumps(resp.get('status_details'))}")
        # An interrupted response can end mid tool call, with half-written arguments: skip those.
        calls = [o for o in resp.get("output", []) if o.get("type") == "function_call"
                 and resp["status"] == "completed"]
        results = []
        for call in calls:
            args = json.loads(call.get("arguments") or "{}")
            self.log(f"  ⚙ {call['name']}({json.dumps(args)[1:-1]})")
            result = await self.tools.run(call["name"], args)
            results.append(result)
            self.log(f"    → {json.dumps(result)[:240]}")
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(result)}}))
        if self.tools.end_requested:
            await self.audio.cut()
            self.audio.add("stopped", chime(STOPPED))  # the session closes once this has played
            self.responding = False
        elif calls and self._spoke(resp) and all(c["name"] in QUIET_AFTER and not needs_reply(r)
                                                 for c, r in zip(calls, results)):
            # It already said "on it" and the tools just did it: a follow-up would only repeat that.
            self.responding = False
            if self.pending_relay or self.want_response:
                self.pending_relay = self.want_response = False
                await ws.send(json.dumps({"type": "response.create"}))
        elif calls:
            await ws.send(json.dumps({"type": "response.create"}))  # stays responding until that one's done
        elif self.pending_relay or self.want_response:
            self.pending_relay = self.want_response = False
            await ws.send(json.dumps({"type": "response.create"}))
        else:
            self.responding = False
