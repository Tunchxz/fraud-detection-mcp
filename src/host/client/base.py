"""The MCP client, and the transport contract its subclasses fill in.

Mirror image of `mcp_core.protocol.McpSession`: that class answers requests, this one asks
them. It opens the handshake, learns what a server offers, and invokes it.

A transport supplies three things (how to open, how to exchange one message, how to close)
and knows nothing about MCP. The exchange is a single operation rather than a send and a
receive. stdio pretends to be a one round trip by writing a line and reading the next.
"""

from __future__ import annotations

from typing import Any

from mcp_core import jsonrpc
from mcp_core.msglog import MessageLog
from mcp_core.protocol import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

# What the host announces about itself in the handshake.
CLIENT_NAME = "fraud-monitoring-host"
CLIENT_VERSION = "0.1.0"


def failed(result: dict) -> bool:
    """Whether a tool result is a refusal.

    `isError` is optional in the protocol and absent means success (official
    servers answers set it only when something went wrong). Reading the key
    directly would raise on them, so everything goes through here.
    """
    return bool(result.get("isError"))


class McpClientError(Exception):
    """Anything that stops this client from talking to its server."""


class McpError(McpClientError):
    """A JSON-RPC error object came back.

    This is a protocol failure: an unknown method, arguments that do not fit the schema. A
    business refusal is not one of these (it arrives as a perfectly successful result
    carrying `isError: true`, and the model is meant to read it and correct itself).
    """

    def __init__(self, server: str, method: str, error: dict) -> None:
        self.server = server
        self.method = method
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(f"{server}: {method} failed [{self.code}] {error.get('message')}")


class McpTransportError(McpClientError):
    """The connection misbehaved: it closed, desynchronised, or answered nonsense."""


class McpClient:
    """One connection to one MCP server.

    Subclasses implement `_open`, `_exchange` and `_close`; everything above that (the
    handshake, the identifier correlation, the message log) is the same for all of them,
    which is what lets the agent treat multiple servers as one catalogue.
    """

    def __init__(self, name: str, log: MessageLog) -> None:
        self.name = name
        self.log = log
        self.next_id = 0

        # Filled in by the handshake.
        self.protocol_version: str = ""
        self.server_info: dict = {}
        self.instructions: str = ""
        self.tools: list[dict] = []

    # --- What a transport must provide ------------------------------------------------

    def _open(self) -> None:
        raise NotImplementedError

    def _exchange(self, raw: str, expects_reply: bool) -> str | None:
        """Sends one raw message; returns the raw answer, or None for a notification."""
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

    # --- JSON-RPC over that transport -------------------------------------------------

    def request(self, method: str, params: dict | None = None) -> Any:
        """Sends a request and returns its result, raising on a JSON-RPC error."""
        self.next_id += 1
        identifier = self.next_id
        raw = jsonrpc.encode(jsonrpc.request_message(identifier, method, params))

        self.log.outbound(raw)
        answer = self._exchange(raw, expects_reply=True)
        if answer is None:
            raise McpTransportError(f"{self.name} sent no answer to {method}")
        self.log.inbound(answer)

        response = jsonrpc.decode_response(answer)
        if response.id != identifier:
            # The pipe is out of step; carrying on would pair answers with the wrong asks.
            raise McpTransportError(
                f"{self.name} answered {method} with id {response.id!r}, expected {identifier!r}"
            )
        if response.error is not None:
            raise McpError(self.name, method, response.error)
        return response.result

    def notify(self, method: str, params: dict | None = None) -> None:
        """Sends a notification. It carries no identifier and is never answered."""
        raw = jsonrpc.encode(jsonrpc.notification_message(method, params))
        self.log.outbound(raw)
        self._exchange(raw, expects_reply=False)

    # --- MCP ---------------------------------------------------------------------------

    def connect(self) -> None:
        """Opens the transport, runs the handshake and reads the tool catalogue."""
        self._open()

        result = self.request(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )

        self.protocol_version = result.get("protocolVersion") or ""
        self.server_info = result.get("serverInfo") or {}
        # Servers may describe their intended workflow here; ours does. The agent puts it
        # in the system prompt, so it is kept rather than dropped.
        self.instructions = result.get("instructions") or ""

        if self.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            # Negotiation is the client's call: a server that answers with a version we do
            # not speak gets disconnected instead of guessed at.
            raise McpTransportError(
                f"{self.name} negotiated {self.protocol_version!r}, which this host does not speak"
            )

        self.notify("notifications/initialized")
        self.tools = self.list_tools()
        self.log.note(
            f"handshake ok · {self.title} · protocol {self.protocol_version} · "
            f"{len(self.tools)} tools"
        )

    def list_tools(self) -> list[dict]:
        tools = self.request("tools/list").get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Invokes a tool and returns its result, `isError: true` included."""
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def ping(self) -> None:
        self.request("ping")

    def close(self) -> None:
        self._close()

    # --- Identity ----------------------------------------------------------------------

    @property
    def title(self) -> str:
        """What to show in the interface: the server's own name, falling back to the key."""
        return self.server_info.get("title") or self.server_info.get("name") or self.name

    def __enter__(self) -> McpClient:
        self.connect()
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()
