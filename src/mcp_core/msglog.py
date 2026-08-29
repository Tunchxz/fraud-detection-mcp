"""Message log.

Every JSON-RPC message that crosses the transport is recorded here. The destination is
`stderr` (which the stdio transport leaves free for logging) and optionally a file when
`FRAUD_MCP_LOG` is set. Nothing is ever written to `stdout` since the specification
reserves it for protocol messages only.

The host keeps one of these per server. `label` says which server a line belongs to;
`sink` hands the line to a second consumer.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

INBOUND = "→"
OUTBOUND = "←"

# Long payloads are trimmed on the console but written whole to the file.
CONSOLE_LIMIT = 800


class MessageLog:
    def __init__(
        self,
        stream: TextIO | None = None,
        path: str | None = None,
        label: str = "",
        sink: Callable[[str], None] | None = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.label = label
        self.sink = sink
        location = path if path is not None else os.environ.get("FRAUD_MCP_LOG")
        self.file: TextIO | None = None
        if location:
            destination = Path(location)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.file = destination.open("a", encoding="utf-8")

    def inbound(self, raw: str) -> None:
        self._write(INBOUND, raw)

    def outbound(self, raw: str) -> None:
        self._write(OUTBOUND, raw)

    def note(self, message: str) -> None:
        """Records something that is not a protocol message, such as a startup line."""
        self._write("·", message)

    def _write(self, direction: str, raw: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        origin = f" {self.label}" if self.label else ""
        line = f"[{stamp}]{origin} {direction} {raw}"

        # The file always gets the whole line; the console gets it trimmed.
        if self.file is not None:
            self.file.write(line + "\n")
            self.file.flush()

        if len(line) > CONSOLE_LIMIT:
            line = line[:CONSOLE_LIMIT] + f"… ({len(raw)} bytes)"

        if self.sink is not None:
            # Whoever asked for the lines is drawing them; printing too would overwrite it.
            self.sink(line)
        else:
            print(line, file=self.stream, flush=True)

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
