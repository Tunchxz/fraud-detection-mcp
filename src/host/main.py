"""The chatbot: command line entry point.

Holds no logic of its own. It reads the environment, opens the servers, builds the agent
and runs the conversation until the analyst leaves.

Run it with:

    uv run python -m host.main            # the fraud server over stdio, as a subprocess
    uv run python -m host.main --remote   # the same server over HTTPS
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.text import Text

from host.agent import Agent
from host.client.base import McpClientError
from host.client.manager import DEFAULT_CONFIG, DEFAULT_LOG, ClientManager
from host.llm import Llm, LlmError
from host.session import Session, build_system_prompt
from host.ui.console import THEME, TerminalView
from host.ui.log_view import LogView

OFFICIAL = ["filesystem", "git"]

HELP = """\
/help            this list
/log [n]         the last n MCP messages in full (20 by default)
/trace           show or hide the live MCP message trace
/tools           every tool available, by server
/servers         what is connected, and what is not
/reset           forget the conversation and start fresh
/quit            leave (Ctrl-D does the same)"""


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="host", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--remote",
        action="store_true",
        help="use the deployed fraud server over HTTPS instead of the local subprocess",
    )
    parser.add_argument("--model", help="override the model (default: $GEMINI_MODEL)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="server list")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="MCP message log")
    parser.add_argument("--no-trace", action="store_true", help="start with the trace hidden")
    return parser.parse_args(argv)


def command(
    word: str, rest: str, view: TerminalView, log: LogView, manager, session, log_path
) -> bool:
    """Runs a slash command. False means the analyst asked to leave."""
    if word in ("/quit", "/exit"):
        return False

    if word == "/help":
        view.note(HELP)
    elif word == "/log":
        log.show(int(rest) if rest.strip().isdigit() else 20, path=str(log_path))
    elif word == "/trace":
        view.note("MCP trace on" if log.toggle() else "MCP trace off")
    elif word == "/tools":
        for tool in manager.tools:
            view.note(f"  {manager.server_of(tool['name']):<13} {tool['name']}")
    elif word == "/servers":
        for key, client in manager.clients.items():
            view.note(f"  {key:<13} {client.title} · {len(client.tools)} tools")
        for key, why in manager.failures:
            view.warning(f"  {key:<13} {why}")
    elif word == "/reset":
        session.reset()
        view.note("Conversation cleared.")
    else:
        view.warning(f"Unknown command {word}. /help lists them.")
    return True


def main(argv: list[str] | None = None) -> int:
    # Before anything else, the manager resolves ${FRAUD_MCP_REMOTE_URL} out of the
    # environment, so a .env loaded any later leaves the remote server silently skipped.
    load_dotenv()
    arguments = parse(argv)

    console = Console(theme=THEME)
    view = TerminalView(console)

    # The model client first (cheap check). Starting four servers only to
    # find out there is no API key would waste the analyst's time.
    try:
        llm = Llm(model=arguments.model)
    except LlmError as error:
        view.error(str(error))
        return 1

    transport = "remote" if arguments.remote else "local"
    # Silent while the servers open: four handshakes are thirty lines of protocol, and
    # burying the welcome panel under them is not good UI/UX. They are in the log all the
    # same, and `/log` shows them.
    log = LogView(console, trace=False)
    manager = ClientManager.from_config(
        arguments.config,
        log_path=arguments.log,
        sink=log,
        select=[*OFFICIAL, f"fraud-{transport}"],
    )

    try:
        manager.open()
    except (McpClientError, OSError) as error:
        view.error(f"Could not open the servers: {error}")
        return 1

    if not manager.clients:
        view.error("No server could be opened. /servers would have nothing to show.")
        manager.close()
        return 1

    session = Session(
        system_prompt=build_system_prompt(manager.instructions(), str(manager.workspace))
    )
    agent = Agent(llm, manager, session, view=view, workspace=manager.workspace)

    view.header(llm.model, transport, manager)
    log.trace = not arguments.no_trace

    try:
        converse(view, log, manager, session, agent, arguments.log)
    finally:
        manager.close()
        view.farewell(arguments.log)
    return 0


def converse(view: TerminalView, log: LogView, manager, session, agent: Agent, log_path) -> None:
    """The loop the analyst actually sees."""
    while True:
        try:
            said = view.ask_analyst().strip()
        except (EOFError, KeyboardInterrupt):
            console_break(view)
            return

        if not said:
            continue
        if said.startswith("/"):
            word, _, rest = said.partition(" ")
            if not command(word, rest, view, log, manager, session, log_path):
                return
            continue

        try:
            agent.ask(said)
        except LlmError as error:
            view.error(str(error))
        except KeyboardInterrupt:
            # Abandons the turn, not the session: the history keeps whatever it reached.
            view.warning("Interrupted.")
        except McpClientError as error:
            view.error(f"A server failed: {error}")


def console_break(view: TerminalView) -> None:
    view.console.print(Text("", style="muted"))


if __name__ == "__main__":
    sys.exit(main())
