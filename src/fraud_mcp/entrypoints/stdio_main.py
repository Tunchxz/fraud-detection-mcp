"""Local entry point: the fraud server over stdio.

Holds no logic of its own. It builds the registry, connects it to the stdio transport and
runs the loop until the host closes standard input.

Everything it reports goes to `stderr`, because `stdout` belongs to the protocol.
"""

from __future__ import annotations

import sys

from fraud_mcp import server
from mcp_core.msglog import MessageLog
from mcp_core.protocol import LATEST_PROTOCOL_VERSION, McpSession
from mcp_core.transport import stdio


def main() -> int:
    log = MessageLog()

    try:
        context = server.build_context()
    except FileNotFoundError as error:
        print(f"fraud-mcp: {error}", file=sys.stderr, flush=True)
        return 1

    registry = server.build_registry(context)
    session = McpSession(registry, server.server_info())

    log.note(
        f"{server.NAME} {server.VERSION} ready on stdio · "
        f"{len(registry.tools)} tools · protocol up to {LATEST_PROTOCOL_VERSION} · "
        f"model trained {context.model.trained_at}"
    )

    try:
        stdio.run(session, stdio.StdioTransport(log))
    except KeyboardInterrupt:
        pass
    finally:
        context.connection.close()
        log.note("connection closed")
        log.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
