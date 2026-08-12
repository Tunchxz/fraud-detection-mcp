"""JSON-RPC 2.0 codec.

Transport module for MCP: message decoding, validation, and serialization.
This module knows about JSON-RPC only. It does not know what MCP is, nor
what the messages mean.

Reference: https://www.jsonrpc.org/specification
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

VERSION = "2.0"

# Standard JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """An error that must be reported back as a JSON-RPC error object.

    Business errors do NOT belong here. The MCP specification requires those to travel as
    a successful result with `isError: true` so the model can correct itself. This class
    is for malformed requests, unknown methods and unexpected failures.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_object(self) -> dict:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


@dataclass(frozen=True)
class Request:
    """An incoming request or notification.

    A notification is a request without `id`; it must not be answered.
    """

    method: str
    params: dict = field(default_factory=dict)
    id: str | int | None = None
    is_notification: bool = False


def decode(line: str) -> Request:
    """Turns one line of text into a request, or raises `JsonRpcError`."""
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JsonRpcError(PARSE_ERROR, "Parse error", str(exc)) from exc

    if not isinstance(message, dict):
        raise JsonRpcError(INVALID_REQUEST, "Request must be a JSON object")
    if message.get("jsonrpc") != VERSION:
        raise JsonRpcError(INVALID_REQUEST, f"Field 'jsonrpc' must be '{VERSION}'")

    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(INVALID_REQUEST, "Field 'method' must be a non-empty string")

    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # The specification allows positional parameters, but MCP never uses them.
        raise JsonRpcError(INVALID_PARAMS, "Field 'params' must be an object")

    # `id` absent means notification. `id` present and null is invalid in JSON-RPC 2.0,
    # but it is accepted here as a notification to stay permissive with clients.
    identifier = message.get("id")
    is_notification = "id" not in message or identifier is None
    if identifier is not None and not isinstance(identifier, (str, int)):
        raise JsonRpcError(INVALID_REQUEST, "Field 'id' must be a string or a number")

    return Request(
        method=method, params=params, id=identifier, is_notification=is_notification
    )


def result_message(request_id: str | int | None, result: Any) -> dict:
    """Builds a successful response."""
    return {"jsonrpc": VERSION, "id": request_id, "result": result}


def error_message(request_id: str | int | None, error: JsonRpcError) -> dict:
    """Builds an error response. `request_id` is null when the id could not be read."""
    return {"jsonrpc": VERSION, "id": request_id, "error": error.to_object()}


def encode(message: dict) -> str:
    """Serializes a message to a single line.

    The stdio transport requires messages with no embedded newlines, so the output is
    compact and any newline inside a string is escaped by the JSON encoder itself.
    """
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
