"""stdio transport for the client: launches the server and talks through its pipes.

The other side of `mcp_core/transport/stdio.py`, and it honours the same contract: one
UTF-8 JSON-RPC message per line, nothing but protocol messages on the pipe, `stderr` free
for the server's own logging, and the client starts the shutdown by closing `stdin`.

Notes:

- `stderr` is drained by a daemon thread. A server that logs a lot would otherwise fill
  that pipe's buffer and block forever, and the host would look hung for no reason. Its
  lines go to the message log, which is where a foreign server's complaints become visible.
- Reads have a deadline, checked with `select`. The official servers are somebody else's
  code; one that never answers must not take the host down with it. `select` on a pipe is
  POSIX, which is where this project runs.
"""

from __future__ import annotations

import os
import select
import subprocess
import threading
from pathlib import Path

from mcp_core.msglog import MessageLog

from host.client.base import McpClient, McpTransportError

# A bit long on purpose as `npx` and `uvx` may download a package on the first run.
DEFAULT_TIMEOUT = 120.0


class StdioClient(McpClient):
    """A server launched as a subprocess, spoken to through its standard pipes."""

    def __init__(
        self,
        name: str,
        log: MessageLog,
        command: list[str],
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(name, log)
        self.command = command
        self.cwd = Path(cwd) if cwd is not None else None
        self.env = dict(os.environ, **(env or {}))
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None

    def _open(self) -> None:
        self.log.note(f"launching {' '.join(self.command)}")
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,  # line buffered: a written message leaves at once
                cwd=self.cwd,
                env=self.env,
            )
        except OSError as error:
            raise McpTransportError(f"{self.name} could not be launched: {error}") from error

        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        """Moves the server's own logging into the message log, so it never blocks."""
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            line = line.rstrip()
            if line:
                self.log.note(f"stderr: {line}")

    def _exchange(self, raw: str, expects_reply: bool) -> str | None:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise McpTransportError(f"{self.name} is not running")

        try:
            process.stdin.write(raw + "\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError) as error:
            raise McpTransportError(f"{self.name} closed its input: {error}") from error

        if not expects_reply:
            return None

        if not select.select([process.stdout], [], [], self.timeout)[0]:
            raise McpTransportError(f"{self.name} did not answer within {self.timeout:g}s")

        line = process.stdout.readline()
        if not line:
            code = process.poll()
            raise McpTransportError(
                f"{self.name} closed its output" + (f" and exited with {code}" if code else "")
            )
        return line.strip()

    def _close(self) -> None:
        process = self.process
        if process is None:
            return

        # Closing stdin is how the contract says a session ends; the server exits with 0.
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass

        try:
            code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait()
        self.log.note(f"closed, exit code {code}")
        self.process = None
