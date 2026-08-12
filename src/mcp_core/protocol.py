"""MCP session: handshake, method dispatch and version negotiation.

This layer knows the protocol and nothing about the domain. It implements the
handshake-based version of MCP (`initialize` → response → `notifications/initialized`)
which is what current hosts speak.

Reference: https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_core.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    JsonRpcError,
    Request,
    error_message,
    result_message,
)
from mcp_core.registry import ToolRegistry

# The server answers with the version the client asked for when it is one of
# these, and with the newest one otherwise (specification requirement).
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


@dataclass(frozen=True)
class ServerInfo:
    name: str
    title: str
    version: str
    instructions: str = ""


class McpSession:
    """Handles one connection. The transport feeds it requests and sends its answers."""

    def __init__(self, registry: ToolRegistry, info: ServerInfo) -> None:
        self.registry = registry
        self.info = info
        self.initialized = False
        self.ready = False  # set by notifications/initialized
        self.protocol_version = LATEST_PROTOCOL_VERSION
        self.client_info: dict[str, Any] = {}

    def handle(self, request: Request) -> dict | None:
        """Returns the response to send, or None when the message is a notification."""
        try:
            result = self._dispatch(request)
        except JsonRpcError as error:
            if request.is_notification:
                return None  # notifications are never answered, not even with errors
            return error_message(request.id, error)
        except Exception as exc:  # noqa: BLE001 - the loop must survive any tool failure
            if request.is_notification:
                return None
            return error_message(
                request.id, JsonRpcError(INTERNAL_ERROR, "Internal error", str(exc))
            )

        if request.is_notification:
            return None
        return result_message(request.id, result)

    def _dispatch(self, request: Request) -> Any:
        method = request.method

        if method == "initialize":
            return self._initialize(request.params)
        if method == "notifications/initialized":
            self.ready = True
            return None
        if method == "ping":
            # Allowed at any point in the lifecycle, before the handshake included.
            return {}

        if not self.initialized:
            raise JsonRpcError(
                INVALID_REQUEST, "Server not initialized: send 'initialize' first"
            )

        if method == "tools/list":
            return {"tools": self.registry.descriptors()}
        if method == "tools/call":
            return self._call_tool(request.params)
        if method.startswith("notifications/"):
            return None  # cancellation, progress and the like: nothing to do here

        raise JsonRpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        if requested is not None and not isinstance(requested, str):
            raise JsonRpcError(INVALID_PARAMS, "protocolVersion must be a string")

        self.protocol_version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        self.client_info = params.get("clientInfo", {}) or {}
        self.initialized = True

        result: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self.info.name,
                "title": self.info.title,
                "version": self.info.version,
            },
        }
        if self.info.instructions:
            result["instructions"] = self.info.instructions
        return result

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS, "Field 'name' must be a non-empty string")

        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "Field 'arguments' must be an object")

        return self.registry.call(name, arguments)
