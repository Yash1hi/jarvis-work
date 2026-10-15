"""What Jarvis has cost. Every paid call adds to a running total kept on disk, which the
menu bar (menubar.swift, Jarvis Controls) and the `spending` tool read.

  logs/sessions.jsonl  one line per finished conversation or announcement
  logs/live.json       the conversation in progress, rewritten after every response
"""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"
SESSIONS = LOGS / "sessions.jsonl"
LIVE = LOGS / "live.json"


class Meter:
    def __init__(self):
        LOGS.mkdir(exist_ok=True)
        LIVE.unlink(missing_ok=True)  # left over from a crash
        self.live = None

    def start(self, model):
        self.live = {"kind": "conversation", "model": model, "started": _now(), "cost": 0.0}
        self._t0 = time.monotonic()
        self._write_live()

    def add(self, usd):
        if self.live:
            self.live["cost"] += usd
            self._write_live()

    def finish(self):
        """Close the live conversation and log it; returns its summary line."""
        entry = {"at": _now(), "kind": "conversation", "model": self.live["model"],
                 "minutes": round((time.monotonic() - self._t0) / 60, 2), "cost": round(self.live["cost"], 4)}
        self._append(entry)
        self.live = None
        LIVE.unlink(missing_ok=True)
        return entry

    def record(self, kind, usd, **extra):
        """A one-off paid call outside a conversation, e.g. a spoken announcement."""
        self._append({"at": _now(), "kind": kind, "cost": round(usd, 4), **extra})

    def totals(self):
        entries = []
        if SESSIONS.exists():
            entries = [json.loads(line) for line in SESSIONS.read_text().splitlines() if line.strip()]
        now = datetime.now()
        today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        live = self.live["cost"] if self.live else 0.0

        def total(keep):
            return round(sum(e["cost"] for e in entries if keep(e["at"])) + live, 2)

        return {
            "today_usd": total(lambda at: at.startswith(today)),
            "this_week_usd": total(lambda at: at[:10] >= week_start),
            "this_month_usd": total(lambda at: at.startswith(month)),
            "all_time_usd": total(lambda at: True),
            "conversations_today": sum(e.get("kind", "conversation") == "conversation" and e["at"].startswith(today)
                                       for e in entries) + bool(self.live),
            "this_conversation_usd": round(live, 3),
            "note": "Estimated from OpenAI's token counts and list prices.",
        }

    def _write_live(self):
        tmp = LIVE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.live))
        os.replace(tmp, LIVE)

    def _append(self, entry):
        with SESSIONS.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def _now():
    return datetime.now().isoformat(timespec="seconds")
