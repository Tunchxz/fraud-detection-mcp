"""The terminal interface."""

from __future__ import annotations

import time
from contextlib import contextmanager

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from host.agent import View

THEME = Theme(
    {
        "analyst": "bold cyan",
        "muted": "dim",
        "heading": "bold",
        "ok": "green",
        "refusal": "bold red",
        "warn": "yellow",
        "fraud": "magenta",  # the project's own server
        "official": "blue",  # the servers somebody else wrote
    }
)

# Enough of an argument list to recognise the call, not enough to wrap the line.
ARGUMENT_WIDTH = 30
ARGUMENTS_WIDTH = 64


def abbreviate(value: object, width: int = ARGUMENT_WIDTH) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def arguments_of(arguments: dict) -> str:
    rendered = ", ".join(f"{key}={abbreviate(value)}" for key, value in arguments.items())
    return abbreviate(rendered, ARGUMENTS_WIDTH)


def tone_of(server: str) -> str:
    return "fraud" if server.startswith("fraud") else "official"


class TerminalView(View):
    """What the agent reports, drawn."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(theme=THEME)
        self.pending: tuple[str, dict] | None = None

    # --- The frame ---------------------------------------------------------------------

    def header(self, model: str, transport: str, manager) -> None:
        """The welcome panel: what is connected, and what is not."""
        table = Table.grid(padding=(0, 2))
        table.add_column(style="heading")
        table.add_column()

        for key, client in manager.clients.items():
            table.add_row(
                Text(key, style=tone_of(key)),
                Text.assemble(
                    (f"{len(client.tools):>2} tools", "muted"),
                    ("  ", ""),
                    (client.title, "muted"),
                ),
            )
        for key, why in manager.failures:
            table.add_row(Text(key, style="warn"), Text(why, style="muted"))

        body = Group(
            Text.assemble(("model  ", "muted"), (model, "heading")),
            Text.assemble(("server ", "muted"), (f"fraud over {transport}", "heading")),
            Text(""),
            table,
            Text(""),
            Text(f"{len(manager.tools)} tools available · /help for commands", style="muted"),
        )
        self.console.print(Panel(body, title="Fraud monitoring assistant", border_style="analyst"))

    def ask_analyst(self) -> str:
        return Prompt.ask(Text("\nyou", style="analyst"), console=self.console)

    def farewell(self, log_path) -> None:
        self.console.print(Text(f"\nMessage log written to {log_path}", style="muted"))

    # --- What the agent reports --------------------------------------------------------

    @contextmanager
    def thinking(self, label: str = "thinking"):
        """A working line with a live stopwatch, erased once the answer arrives."""
        started = time.monotonic()
        spinner = Spinner("dots", style="muted")

        def frame() -> Text:
            elapsed = time.monotonic() - started
            return Text.assemble(
                spinner.render(time.monotonic()), " ", (label, "muted"), (f"  {elapsed:.0f}s", "muted")
            )

        with Live(get_renderable=frame, console=self.console, refresh_per_second=12, transient=True):
            yield

    def tool_started(self, server: str, tool: str, arguments: dict) -> None:
        # Remembered, not drawn: the finished line carries the whole call, and two lines
        # per tool would bury the conversation.
        self.pending = (tool, arguments)

    def tool_finished(self, server: str, tool: str, seconds: float, ok: bool, summary: str) -> None:
        arguments = self.pending[1] if getattr(self, "pending", None) and self.pending[0] == tool else {}
        self.pending = None
        self.console.print(
            Text.assemble(
                ("● " if ok else "✗ ", "ok" if ok else "refusal"),
                (f"{server:<13}", tone_of(server)),
                (tool, "heading"),
                (f"({arguments_of(arguments)})", "muted"),
                (f"  {seconds * 1000:.0f} ms", "muted"),
                ("  " + abbreviate(summary, 56) if summary else "", "muted"),
            ),
            # One call, one line, whatever the terminal is wide.
            no_wrap=True,
            overflow="ellipsis",
        )

    def confirm(self, server: str, tool: str, arguments: dict) -> bool:
        """The one place the analyst has to say yes before something changes."""
        body = Group(
            Text.assemble((tool, "heading"), ("  on ", "muted"), (server, tone_of(server))),
            Text(""),
            *[
                Text.assemble((f"{key:<20}", "muted"), (abbreviate(value, 60), ""))
                for key, value in arguments.items()
            ],
        )
        self.console.print(Panel(body, title="Confirm", border_style="warn"))
        return Confirm.ask(Text("Go ahead?", style="warn"), console=self.console, default=False)

    def answer(self, text: str) -> None:
        if text:
            self.console.print()
            self.console.print(Markdown(text))

    def warning(self, text: str) -> None:
        self.console.print(Text.assemble(("! ", "warn"), (text, "warn")))

    def error(self, text: str) -> None:
        self.console.print(Text.assemble(("✗ ", "refusal"), (text, "refusal")))

    def note(self, text: str) -> None:
        self.console.print(Text(text, style="muted"))
