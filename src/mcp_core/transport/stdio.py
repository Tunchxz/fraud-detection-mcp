"""stdio transport.

The normative requirements of the MCP stdio transport, all enforced here:

- messages are UTF-8 and delimited by newlines, and **must not** contain embedded newlines;
- the server reads requests from `stdin` and writes messages to `stdout`;
- nothing that is not a valid MCP message may be written to `stdout`;
- `stderr` is free for logging.

The layer above (`mcp_core.protocol`) decides what the messages mean; this one only turns
bytes into lines and back.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import TextIO

from mcp_core import jsonrpc
from mcp_core.msglog import MessageLog
from mcp_core.protocol import McpSession


class StdioTransport:
    def __init__(
        self,
        log: MessageLog,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self.log = log
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout

        # The specification mandates UTF-8 regardless of the platform's default encoding.
        for stream in (self.stdin, self.stdout):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", newline="\n")

    def lines(self) -> Iterator[str]:
        """Yields one raw message per line until the client closes `stdin`."""
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue  # blank lines are not messages
            self.log.inbound(line)
            yield line

    def send(self, message: dict) -> None:
        raw = jsonrpc.encode(message)
        self.log.outbound(raw)
        self.stdout.write(raw + "\n")
        self.stdout.flush()


def run(session: McpSession, transport: StdioTransport) -> None:
    """Reads messages until end of input, answering each request in order.

    A message that cannot even be parsed is answered with a parse error carrying a null
    id, which is the only case where the id is unknown.
    """
    for line in transport.lines():
        try:
            request = jsonrpc.decode(line)
        except jsonrpc.JsonRpcError as error:
            transport.send(jsonrpc.error_message(None, error))
            continue

        response = session.handle(request)
        if response is not None:
            transport.send(response)
