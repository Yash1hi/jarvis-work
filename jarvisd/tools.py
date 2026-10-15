"""What Jarvis can do: the tool schemas the Realtime model sees, and their implementations.

Jarvis itself only talks. The work goes to background agents (agents.py), which have Yash's whole
Claude Code setup, so the voice side stays a handful of quick tools.
"""
import asyncio
import re
from datetime import datetime

import qrcode
import qrcode.image.svg

from . import agents, phone
from .workspace import kind_of

SESSION = {"type": "string", "description": "Its handle or name from agents, or the project."}
SIZE = {"type": "string", "enum": ["small", "medium", "large", "wide", "tall", "reset"],
        "description": "Panel size to fit what's on it: large for web pages, images and long reads, wide for wide "
                       "tables, tall for long lists, small when there's little on it, reset for Yash's own size."}

SCHEMAS = [
    {
        "type": "function",
        "name": "get_time",
        "description": "Current local date and time.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "agents",
        "description": "What Claude Code and Codex are doing on this Mac: background tasks Jarvis started and "
                       "Yash's own Terminal sessions, each with its handle, project, state and the last thing it said.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "agent_start",
        "description": "Hand a task to a background agent and get on with the conversation; Jarvis announces "
                       "when it finishes. Claude has Yash's full work setup (Scorecard, GitHub, Figma, Google "
                       "Workspace and the rest of his tools). Without a project it's an errand: look something "
                       "up, summarise, check, draft. With a project it works in that repo.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The instruction, written out as Yash would type it."},
                "project": {"type": "string", "description": "A repo folder, e.g. 'docs' or 'jarvis', when the task "
                            "is about one. Leave it out for everything else."},
                "agent": {"type": "string", "enum": ["claude", "codex"],
                          "description": "Claude unless Yash says Codex. Codex has no access to his work tools."},
            },
            "required": ["task"],
        },
    },
    {
        "type": "function",
        "name": "agent_result",
        "description": "An agent's full last message, for when Yash wants more than the announcement.",
        "parameters": {"type": "object", "properties": {"session": SESSION}, "required": ["session"]},
    },
    {
        "type": "function",
        "name": "agent_send",
        "description": "Give an agent a follow-up instruction or answer its question. For Yash's own Terminal "
                       "sessions this types it into the tab as if he had, so the first call only returns the "
                       "message to read back; send it with confirmed=true after he says yes.",
        "parameters": {
            "type": "object",
            "properties": {
                "session": SESSION,
                "message": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "True only after Yash agreed to this exact message."},
            },
            "required": ["session", "message"],
        },
    },
    {
        "type": "function",
        "name": "agent_open",
        "description": "Bring an agent's session up in Terminal so Yash can read it or take over by hand.",
        "parameters": {"type": "object", "properties": {"session": SESSION}, "required": ["session"]},
    },
    {
        "type": "function",
        "name": "agent_stop",
        "description": "Stop a background agent (never Yash's own Terminal sessions).",
        "parameters": {"type": "object", "properties": {"session": SESSION}, "required": ["session"]},
    },
    {
        "type": "function",
        "name": "show",
        "description": "Put something on Yash's workspace panel (top right of his screen), replacing what was there. "
                       "markdown: lists, tables, numbers, summaries, drafts; it can include images, video, PDFs and "
                       "HTML charts as ![caption](address), where address is a web address or a file path on his "
                       "Mac. session: an agent's full answer as it wrote it (with any images it made). url: one "
                       "image, video, PDF or file on its own, or a web page, which opens in a browser view in "
                       "the panel (he can scroll and click in it).",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A few words, e.g. 'Failed runs today'."},
                "markdown": {"type": "string"},
                "session": {"type": "string", "description": "An agent's handle, to show its last answer."},
                "url": {"type": "string", "description": "A web address or a file path."},
                "size": SIZE,
            },
        },
    },
    {
        "type": "function",
        "name": "workspace",
        "description": "Run the workspace panel: bring it back or hide it, clear it, resize it, change how "
                       "see-through it is, or turn click-through on or off (on: clicks and scrolling pass to the "
                       "window underneath, so Yash can't use the panel). Returns its state afterwards.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["show", "hide", "clear", "resize", "opacity", "click_through",
                                                      "status"]},
                "on": {"type": "boolean", "description": "For click_through."},
                "size": SIZE,
                "opacity": {"type": "number", "description": "For opacity: 0.15 (faint) to 1 (solid glass)."},
                "change": {"type": "number", "description": "For opacity, relative instead: e.g. -0.2 is more "
                           "see-through, 0.2 more solid."},
            },
            "required": ["action"],
        },
    },
    {
        "type": "function",
        "name": "phone_link",
        "description": "Put a QR code on the panel that opens Jarvis on Yash's iPhone, so Jarvis uses the phone's mic "
                       "and his headphones instead of the Mac's.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "spending",
        "description": "What Jarvis has cost in OpenAI usage: today, this week, this month, all time, "
                       "and this conversation so far.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "end_conversation",
        "description": "Yash is done (go away / that's all / thanks / stop listening). Say nothing: a chime plays.",
        "parameters": {"type": "object", "properties": {}},
    },
]


class Tools:
    def __init__(self, meter, agents, ui, log, phone=None):
        self.meter, self.agents, self.ui, self.log, self.phone = meter, agents, ui, log, phone
        self.end_requested = False

    async def run(self, name, args):
        handler = getattr(self, f"_{name}", None)
        if not handler:
            return {"error": f"unknown tool {name}"}
        try:
            return await handler(**args)
        except Exception as e:  # tell the model, so it can say what went wrong
            self.log(f"tool {name} failed: {e}")
            return {"error": str(e)}

    async def _get_time(self):
        return {"now": datetime.now().strftime("%A %-d %B %Y, %-I:%M %p")}

    async def _agents(self):
        sessions = await asyncio.to_thread(self.agents.sessions)
        return {"sessions": [agents.public(s) for s in sessions]}

    async def _agent_start(self, task, project=None, agent="claude"):
        return await asyncio.to_thread(self.agents.start, agent, task, project)

    async def _agent_result(self, session):
        return await asyncio.to_thread(self.agents.result, session)

    async def _agent_send(self, session, message, confirmed=False):
        return await asyncio.to_thread(self.agents.send, session, message, confirmed)

    async def _agent_open(self, session):
        return await asyncio.to_thread(self.agents.open, session)

    async def _agent_stop(self, session):
        return await asyncio.to_thread(self.agents.stop, session)

    async def _show(self, title=None, markdown=None, session=None, url=None, size=None):
        if session:
            heading, markdown = await asyncio.to_thread(self.agents.raw_result, session)
            if markdown is None:
                return {"error": heading}
            title = title or heading
        elif url:
            local = not re.match(r"https?://", url)
            kind = kind_of(url)
            if kind == "page" and local:
                return {"error": f"Can't show {url}: only images, video, PDFs and HTML files can go on the panel."}
            if kind in ("page", "html"):  # a web page, or an HTML file (a chart, a report): the browser view
                address = self.ui.file_url(url) if local else url
                if not address:
                    return {"error": f"Can't open {url}: it doesn't exist, or it's outside the home folder."}
                self.ui.open_web(f"http://127.0.0.1:{self.ui.port}{address}" if local else address, title)
                state = await self.ui.panel_report(f"size {size or 'large'}")
                return {"showing": title or url, "as": "a web page in the panel", "panel": state}
            markdown = f"![{title or ''}]({url})"
        if not markdown:
            return {"error": "Nothing to show: pass markdown, session or url."}
        missing = self.ui.show(title, markdown)
        result = {"showing": title or "it", "on": "the workspace panel",
                  "panel": await self.ui.panel_report(f"size {size}" if size else None)}
        if missing:
            result["not_shown"] = {"files": missing, "why": "they don't exist, or they're outside the home folder"}
        return result

    async def _workspace(self, action, size=None, opacity=None, change=None, on=None):
        if action == "clear":
            self.ui.clear()
            return {"cleared": True, "panel": await self.ui.panel_report()}
        if action == "resize":
            if not size:
                return {"error": "Say which size: small, medium, large, wide, tall or reset."}
            return {"panel": await self.ui.panel_report(f"size {size}")}
        if action == "opacity":
            if change is not None:
                return {"panel": await self.ui.panel_report(f"opacity {float(change):+.2f}")}
            if opacity is None:
                return {"error": "Give an opacity from 0.15 to 1, or a change like -0.2."}
            return {"panel": await self.ui.panel_report(f"opacity {min(1.0, max(0.15, float(opacity))):.2f}")}
        if action in ("show", "hide"):
            return {"panel": await self.ui.panel_report(action)}
        if action == "click_through":
            return {"panel": await self.ui.panel_report(f"clickthrough {'on' if on else 'off'}")}
        return {"panel": await self.ui.panel_report()}

    async def _phone_link(self):
        link = self.phone and await asyncio.to_thread(lambda: self.phone.link)
        if not link:
            return {"error": "This Mac isn't on Tailscale, so the phone can't reach Jarvis."}
        if not await asyncio.to_thread(phone.serving):
            return {"error": "Not set up yet. Yash needs to turn on HTTPS certificates in the Tailscale admin console "
                             "(DNS page), then run: tailscale serve --bg 8793"}
        image = phone.KEY_FILE.with_name("phone-link.svg")
        qrcode.make(link, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=20, border=2).save(image)
        self.ui.show("Jarvis on your iPhone", "Scan it with the iPhone camera, open it in Safari and tap **Start**. "
                     "Then Share › **Add to Home Screen** to keep it like an app.\n\n"
                     f"![Opens Jarvis on your iPhone]({image})")
        return {"showing": "a QR code for the phone page", "panel": await self.ui.panel_report("size medium")}

    async def _spending(self):
        return self.meter.totals()

    async def _end_conversation(self):
        self.end_requested = True
        return {"ending": True}
