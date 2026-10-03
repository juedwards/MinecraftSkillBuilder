"""Terminal output: a startup banner and one aligned, colour-coded line per event.

Colour is only used on a terminal: output redirected to a file (mcchat.log) or a hosting platform's
log stream stays plain text, and NO_COLOR (https://no-color.org) turns it off everywhere.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, TextIO

Event = dict[str, Any]

TAG_WIDTH = 7          # widest tag is "CONFIG"/"RESULT", plus a space
TIME_WIDTH = 8         # HH:MM:SS
INDENT = TIME_WIDTH + 2 + TAG_WIDTH + 1   # where the player name starts

# Tag text and colour (ANSI SGR codes) for each event type.
TAGS = {
    "question": ("CHAT", "36"),        # cyan
    "answer": ("AI", "35"),            # magenta
    "build": ("BUILD", "33"),          # yellow
    "assess": ("QUEST", "32"),         # green
    "assessment": ("RESULT", "1;32"),  # bold green
    "setup": ("SETUP", "34"),          # blue
    "settings": ("CONFIG", "34"),
    "connected": ("LINK", "32"),
    "disconnected": ("LINK", "90"),    # grey
    "reset": ("RESET", "90"),
    "error": ("ERROR", "1;31"),        # bold red
    "info": ("INFO", "90"),
    "assistant": ("ASSIST", "94"),     # light blue: the teacher's assistant in this terminal
}

LEVEL_COLOURS = {"beginning": "31", "developing": "33", "secure": "32", "excellent": "1;36"}

# The four blues of the web interface's logo (256-colour palette).
LOGO = (("27", "75"), ("19", "33"))


def _supports_unicode(stream: TextIO) -> bool:
    try:
        "─›→●○█".encode(stream.encoding or "ascii")
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def _wants_colour(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"


@dataclass
class Console:
    stream: TextIO | None  # None: whatever sys.stdout is when writing (the interactive prompt swaps it)
    colour: bool
    unicode: bool
    held: list[str] | None = field(default=None, repr=False)

    @classmethod
    def for_stream(cls, stream: TextIO | None = None) -> "Console":
        target = stream or sys.stdout
        colour = _wants_colour(target)
        if colour and os.name == "nt":
            os.system("")  # turns on ANSI escape handling in the classic Windows console
        return cls(stream, colour, _supports_unicode(target))

    # ----- low-level helpers -----

    def style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour and code else text

    def sym(self, fancy: str, plain: str) -> str:
        return fancy if self.unicode else plain

    def width(self) -> int:
        if not self.colour:
            return 0  # not a terminal: never wrap, so each event stays on one greppable line
        return max(60, shutil.get_terminal_size((100, 24)).columns)

    def write(self, text: str = "") -> None:
        if self.held is not None:
            self.held.append(text)
            return
        print(text, file=self.stream or sys.stdout, flush=True)

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Keep output back (e.g. while an editor has the screen) and print it afterwards."""
        self.held = []
        try:
            yield
        finally:
            held, self.held = self.held, None
            for text in held:
                self.write(text)

    def block(self, kind: str, text: str, tone: str = "") -> None:
        """Several lines (an assistant reply, a quest): each wrapped and indented under the first."""
        lines = text.strip("\n").splitlines() or [""]
        self.line(kind, "", lines[0], tone)
        for raw in lines[1:]:
            stripped = raw.rstrip()
            extra = len(stripped) - len(stripped.lstrip())
            for part in self._wrap(stripped.strip(), INDENT + extra) if stripped else [""]:
                self.write(" " * (INDENT + extra) + self.style(part, tone) if part else "")

    # ----- event lines -----

    def line(self, kind: str, subject: str, text: str, tone: str = "") -> None:
        """`HH:MM:SS  TAG     subject › text`, with text wrapped under itself on a narrow terminal."""
        tag, tag_colour = TAGS.get(kind, (kind.upper()[:TAG_WIDTH - 1], ""))
        stamp = self.style(f"{datetime.now():%H:%M:%S}", "2")
        head = f"{stamp}  {self.style(f'{tag:<{TAG_WIDTH}}', tag_colour)} "
        lead = ""
        if subject:
            lead = f"{subject} {self.sym('›', '>')} "
        body_lines = self._wrap(text, INDENT + len(lead))
        styled_lead = ""
        if subject:
            styled_lead = f"{self.style(subject, '1')} {self.style(self.sym('›', '>'), '2')} "
        first, rest = body_lines[0], body_lines[1:]
        self.write(head + styled_lead + self.style(first, tone))
        for more in rest:
            self.write(" " * (INDENT + len(lead)) + self.style(more, tone))

    def _wrap(self, text: str, indent: int) -> list[str]:
        width = self.width()
        text = " ".join(text.split()) if width else text.replace("\n", " ")
        if not width or len(text) + indent <= width:
            return [text]
        return textwrap.wrap(text, width=width - indent, break_on_hyphens=False) or [""]

    def event(self, event: Event) -> None:
        kind = event["type"]
        player = event.get("player", "")
        if kind == "question":
            self.line(kind, player, event["text"])
        elif kind == "answer":
            self.line(kind, f"{self.sym('→', '->')} {player}", event["text"], tone="2")
        elif kind == "error":
            self.line(kind, player, f"AI request failed: {event['error']}", tone="31")
        elif kind in ("setup", "build", "assess"):
            self.line(kind, player, event["status"], tone=status_tone(event["status"]))
        elif kind == "assessment":
            self.assessment(event)
        elif kind == "connected":
            self.line(kind, "", f"Minecraft connected from {event['remote']}", tone="32")
        elif kind == "disconnected":
            self.line(kind, "", f"Minecraft disconnected ({event['remote']})", tone="2")
        elif kind == "settings":
            self.line(kind, "", event["status"])
        elif kind == "reset":
            self.line(kind, player, "conversation cleared")

    def assessment(self, event: Event) -> None:
        self.line("assessment", event["player"], f"{event['rubric']}, attempt {event['attempt']}", tone="1")
        criteria = event["criteria"]
        name_width = max((len(c["name"]) for c in criteria), default=0)
        pad = " " * (INDENT + 2)
        for c in criteria:
            level = c["level"]
            colour = LEVEL_COLOURS.get(str(level).lower(), "")
            dots = self.style(self.sym("·", ".") * (name_width - len(c["name"]) + 2), "2")
            self.write(f"{pad}{c['name']} {dots} {self.style(level, colour)}")
        if event.get("report"):
            self.write(f"{pad}{self.style('report: ' + str(event['report']), '2')}")

    def info(self, text: str, tone: str = "") -> None:
        self.line("info", "", text, tone=tone)

    # ----- banner -----

    def banner(self, title: str, subtitle: str, version: str, rows: list[tuple[str, str]], footer: str) -> None:
        """App name, then label/value rows (a value can span several lines), then a rule."""
        self.write()
        name = f"{self.style(title, '1')}  {self.style('v' + version, '2')}" if version else self.style(title, "1")
        if self.colour and self.unicode:
            block = lambda c: self.style("██", f"38;5;{c}")
            self.write(f"  {block(LOGO[0][0])}{block(LOGO[0][1])}  {name}")
            self.write(f"  {block(LOGO[1][0])}{block(LOGO[1][1])}  {self.style(subtitle, '2')}")
        else:
            self.write(f"  {name}")
            self.write(f"  {self.style(subtitle, '2')}")
        self.write()
        label_width = max((len(label) for label, _ in rows), default=0) + 3
        for label, value in rows:
            for i, part in enumerate(value.split("\n")):
                shown = label if i == 0 else ""
                self.write(f"  {self.style(f'{shown:<{label_width}}', '2')}{part}")
        self.write()
        self.write(f"  {self.style(footer, '2')}")
        rule_width = min(self.width() or 72, 100) - 2
        self.write(" " + self.style(self.sym("─", "-") * rule_width, "2"))

    def ok(self, text: str) -> str:
        return self.style(f"{self.sym('●', '*')} {text}", "32")

    def warn(self, text: str) -> str:
        return self.style(f"{self.sym('○', 'o')} {text}", "33")

    def code(self, text: str) -> str:
        return self.style(text, "1;36")


def status_tone(status: str) -> str:
    """Green when something is done (yellow if some commands failed), red for failures."""
    lowered = status.lower()
    if lowered.startswith("done") or ": done" in lowered:
        failures = [int(n) for n in re.findall(r"(\d+)\s+(?:commands?\s+)?failed", lowered)]
        return "33" if any(failures) else "32"
    if "failed" in lowered or lowered.startswith("could not"):
        return "31"
    if lowered.startswith(("finished", "attempt")):
        return "32"
    return ""


def describe_os_error(exc: OSError) -> str:
    """Plain-language message for errors starting the server, mainly a port already in use."""
    text = str(exc)
    if "address already in use" in text.lower() or getattr(exc, "errno", None) in (98, 48, 10048):
        match = re.search(r",\s*(\d+)\)", text)
        port = f"Port {match.group(1)} is" if match else "A port is"
        return (f"{port} already in use. Quest Builder may already be running in another window: "
                "stop it first, or choose other ports with --port and --web-port.")
    return text
