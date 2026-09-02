"""The MCP message log, on screen.

A log of all the interactions with the MCP servers, requests and answers alike, kept and
shown. Keeping it is already done: every client writes through `mcp_core.msglog.MessageLog`,
which appends to `logs/mcp-session.log`. This is the other half.

It plugs in as that class' `sink`, and handing over a sink also stops the library printing
to `stderr`, so this view owns the drawing. Two ways to read the same thing: a dim
one-line trace while the conversation runs, which is what makes the protocol visible during
a demonstration, and `/log`, which prints the last messages in full.
"""

from __future__ import annotations

import re
from collections import deque

from rich.console import Console
from rich.text import Text

# What `MessageLog` writes: "[12:34:56.789] fraud-local → {json}". The direction glyph is
# from the server's point of view, so with a label the line reads "to it" / "from it".
LINE = re.compile(r"^\[(?P<stamp>[\d:.]+)\]\s+(?:(?P<label>\S+)\s+)?(?P<arrow>[→←·])\s(?P<body>.*)$")

TONES = {"→": "ok", "←": "analyst", "·": "muted"}
KEPT = 800


class LogView:
    """Remembers every MCP message of the session and knows how to show it."""

    def __init__(self, console: Console, trace: bool = True, kept: int = KEPT) -> None:
        self.console = console
        self.trace = trace
        self.lines: deque[str] = deque(maxlen=kept)

    def __call__(self, line: str) -> None:
        """The sink. One already formatted line per message."""
        self.lines.append(line)
        # Notes (a server's own logging drained from its stderr, a handshake summary) are
        # kept but not traced. Our own server echoes every message to stderr, so tracing
        # them would show each one twice, and the trace is meant to show the protocol.
        if self.trace and self.is_protocol(line):
            self.console.print(self.render(line), no_wrap=True, overflow="ellipsis")

    @staticmethod
    def is_protocol(line: str) -> bool:
        match = LINE.match(line)
        return match is not None and match["arrow"] in ("→", "←")

    def render(self, line: str) -> Text:
        match = LINE.match(line)
        if match is None:
            return Text(line, style="muted")

        label = match["label"] or ""
        arrow = match["arrow"]
        return Text.assemble(
            ("  ", ""),
            (f"{label:<13}", "muted"),
            (f"{arrow} ", TONES.get(arrow, "muted")),
            (match["body"], "muted"),
        )

    def show(self, count: int = 20, path: str | None = None) -> None:
        """`/log`: the last exchanges, one line each.

        Requests and answers only. A `tools/list` answer runs to hundreds of bytes, so the
        lines are clipped to the terminal; if a full payload is needed, it can be found in
        the log file, which is never clipped.
        """
        protocol = [line for line in self.lines if self.is_protocol(line)]
        if not protocol:
            self.console.print(Text("No MCP messages yet.", style="muted"))
            return

        recent = protocol[-count:]
        self.console.print(
            Text(f"Last {len(recent)} of {len(protocol)} MCP messages", style="heading")
        )
        for line in recent:
            self.console.print(self.render(line), no_wrap=True, overflow="ellipsis")
        if path:
            self.console.print(Text(f"  full payloads in {path}", style="muted"))

    def toggle(self) -> bool:
        self.trace = not self.trace
        return self.trace
