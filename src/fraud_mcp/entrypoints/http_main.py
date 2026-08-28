"""Remote entry point: the fraud server over HTTP.

Holds no logic of its own. It builds the registry and connects it to the HTTP transport.

Everything it reports goes to `stderr`, which is where the platform collects logs.
"""

from __future__ import annotations

import os
import sys

from fraud_mcp import server
from mcp_core.msglog import MessageLog
from mcp_core.protocol import LATEST_PROTOCOL_VERSION
from mcp_core.transport import http

DEFAULT_PORT = 8080         # only for running the remote server locally.
DEFAULT_HOST = "0.0.0.0"    # for a container to listen on every interface.


def main() -> int:
    log = MessageLog()

    host = os.environ.get("FRAUD_MCP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT") or DEFAULT_PORT)
    api_key = os.environ.get("FRAUD_MCP_API_KEY")
    origins = [
        origin.strip()
        for origin in os.environ.get("FRAUD_MCP_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]

    try:
        # The connection is reached from whichever thread serves the request.
        context = server.build_context(log, same_thread=False)
    except FileNotFoundError as error:
        print(f"fraud-mcp: {error}", file=sys.stderr, flush=True)
        return 1

    registry = server.build_registry(context)
    transport = http.HttpTransport(
        log, registry, server.server_info(), api_key=api_key, allowed_origins=origins
    )

    if api_key is None:
        log.note(
            "FRAUD_MCP_API_KEY is not set. The endpoint answers without a shared secret. "
            "Acceptable locally, but not in the deployed service."
        )

    log.note(
        f"{server.NAME} {server.VERSION} ready on http://{host}:{port}{http.MCP_PATH} · "
        f"{len(registry.tools)} tools · protocol up to {LATEST_PROTOCOL_VERSION} · "
        f"model trained {context.model.trained_at}"
    )

    try:
        http.run(transport, host, port)
    except KeyboardInterrupt:
        pass
    finally:
        context.connection.close()
        log.note("connection closed")
        log.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
