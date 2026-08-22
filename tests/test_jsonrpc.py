"""The JSON-RPC codec and the session's dispatch rules."""

from __future__ import annotations

import json

import pytest

from mcp_core import jsonrpc
from mcp_core.protocol import LATEST_PROTOCOL_VERSION, McpSession, ServerInfo
from mcp_core.registry import Tool, ToolRegistry, ToolResult


def make_session() -> McpSession:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            title="Echo",
            description="Returns what it is given.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string", "minLength": 2}},
                "required": ["value"],
                "additionalProperties": False,
            },
            handler=lambda value: ToolResult(text=value, data={"value": value}),
        )
    )
    return McpSession(registry, ServerInfo(name="test", title="Test", version="0.0.1"))


def initialize(session: McpSession, version: str = LATEST_PROTOCOL_VERSION) -> dict:
    request = jsonrpc.decode(
        json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}},
        })
    )
    return session.handle(request)


def call(session: McpSession, method: str, params: dict | None = None, identifier: int = 9) -> dict:
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return session.handle(jsonrpc.decode(json.dumps(message)))


# --- Codec -------------------------------------------------------------------------


def test_decode_reads_a_request():
    request = jsonrpc.decode('{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}')
    assert (request.method, request.id, request.is_notification) == ("ping", 7, False)


def test_decode_treats_a_message_without_id_as_a_notification():
    request = jsonrpc.decode('{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert request.is_notification is True


def test_decode_rejects_broken_json():
    with pytest.raises(jsonrpc.JsonRpcError) as raised:
        jsonrpc.decode("{not json")
    assert raised.value.code == jsonrpc.PARSE_ERROR


@pytest.mark.parametrize(
    "message",
    [
        '{"id":1,"method":"ping"}',  # no jsonrpc field
        '{"jsonrpc":"1.0","id":1,"method":"ping"}',  # wrong version
        '{"jsonrpc":"2.0","id":1}',  # no method
        '["jsonrpc","2.0"]',  # not an object
    ],
)
def test_decode_rejects_invalid_requests(message):
    with pytest.raises(jsonrpc.JsonRpcError) as raised:
        jsonrpc.decode(message)
    assert raised.value.code == jsonrpc.INVALID_REQUEST


def test_decode_rejects_positional_params():
    with pytest.raises(jsonrpc.JsonRpcError) as raised:
        jsonrpc.decode('{"jsonrpc":"2.0","id":1,"method":"ping","params":[1,2]}')
    assert raised.value.code == jsonrpc.INVALID_PARAMS


def test_encode_never_emits_a_newline():
    # The stdio transport delimits messages by newlines, so an embedded one would split a
    # message in two.
    raw = jsonrpc.encode(jsonrpc.result_message(1, {"text": "first\nsecond"}))
    assert "\n" not in raw
    assert json.loads(raw)["result"]["text"] == "first\nsecond"


# --- Lifecycle ---------------------------------------------------------------------


def test_initialize_echoes_a_supported_version():
    session = make_session()
    result = initialize(session, "2024-11-05")["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "test"


def test_initialize_falls_back_to_the_newest_version():
    session = make_session()
    result = initialize(session, "1900-01-01")["result"]
    assert result["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_requests_before_initialize_are_refused():
    session = make_session()
    error = call(session, "tools/list")["error"]
    assert error["code"] == jsonrpc.INVALID_REQUEST


def test_ping_works_before_initialize():
    session = make_session()
    assert call(session, "ping")["result"] == {}


def test_notifications_are_never_answered():
    session = make_session()
    initialize(session)
    request = jsonrpc.decode('{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert session.handle(request) is None
    assert session.ready is True


def test_unknown_method_is_reported_as_method_not_found():
    session = make_session()
    initialize(session)
    assert call(session, "resources/list")["error"]["code"] == jsonrpc.METHOD_NOT_FOUND


# --- Tools -------------------------------------------------------------------------


def test_tools_list_reports_the_registered_tools():
    session = make_session()
    initialize(session)
    tools = call(session, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["echo"]
    assert tools[0]["inputSchema"]["required"] == ["value"]


def test_tools_call_returns_content_and_structured_data():
    session = make_session()
    initialize(session)
    result = call(session, "tools/call", {"name": "echo", "arguments": {"value": "hello"}})["result"]
    assert result["content"] == [{"type": "text", "text": "hello"}]
    assert result["isError"] is False
    assert result["structuredContent"] == {"value": "hello"}


def test_unknown_tool_is_a_protocol_error():
    session = make_session()
    initialize(session)
    error = call(session, "tools/call", {"name": "nope", "arguments": {}})["error"]
    assert error["code"] == jsonrpc.METHOD_NOT_FOUND


@pytest.mark.parametrize(
    "arguments",
    [
        {},  # missing required field
        {"value": 5},  # wrong type
        {"value": "x"},  # too short
        {"value": "ok", "extra": 1},  # unknown field
    ],
)
def test_invalid_arguments_are_protocol_errors(arguments):
    session = make_session()
    initialize(session)
    error = call(session, "tools/call", {"name": "echo", "arguments": arguments})["error"]
    assert error["code"] == jsonrpc.INVALID_PARAMS


def test_schema_defaults_are_applied():
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="paged",
            title="Paged",
            description="Uses a default.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 25}},
                "additionalProperties": False,
            },
            handler=lambda limit: ToolResult(text=str(limit)),
        )
    )
    assert registry.call("paged", {})["content"][0]["text"] == "25"


def test_a_failing_tool_does_not_break_the_session():
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="boom",
            title="Boom",
            description="Always fails.",
            input_schema={"type": "object", "additionalProperties": False},
            handler=lambda: (_ for _ in ()).throw(RuntimeError("kaboom")),
        )
    )
    session = McpSession(registry, ServerInfo(name="test", title="Test", version="0.0.1"))
    initialize(session)

    error = call(session, "tools/call", {"name": "boom", "arguments": {}})["error"]
    assert error["code"] == jsonrpc.INTERNAL_ERROR
    # The session survives and keeps answering.
    assert call(session, "ping")["result"] == {}
