"""Tool registry and input validation.

The MCP specification requires servers to validate every tool input, so this module
carries a small JSON Schema validator covering the keywords the server actually uses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp_core.jsonrpc import INVALID_PARAMS, METHOD_NOT_FOUND, JsonRpcError

_TYPE_NAMES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


@dataclass
class ToolResult:
    """What a tool handler returns.

    `text` is the human-readable summary the model reads; `data` carries the same
    information structured, for clients that use it. `is_error` marks a business error:
    the call succeeded at the protocol level but the operation was refused, which is how
    the specification wants domain failures reported so the model can self-correct.
    """

    text: str
    data: dict | None = None
    is_error: bool = False

    def to_payload(self) -> dict:
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": self.text}],
            "isError": self.is_error,
        }
        if self.data is not None:
            payload["structuredContent"] = self.data
        return payload


@dataclass(frozen=True)
class Tool:
    """A callable exposed to the model."""

    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[..., ToolResult]

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class ToolRegistry:
    """Holds the tools of a server, in registration order."""

    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self.tools[tool.name] = tool

    def descriptors(self) -> list[dict]:
        """Tool list for `tools/list`; the order is stable across calls."""
        return [tool.descriptor() for tool in self.tools.values()]

    def call(self, name: str, arguments: dict) -> dict:
        """Validates the arguments and runs the tool.

        An unknown tool or invalid arguments are protocol errors; anything the tool itself
        refuses comes back inside the result with `isError: true`.
        """
        tool = self.tools.get(name)
        if tool is None:
            raise JsonRpcError(METHOD_NOT_FOUND, f"Unknown tool: {name}")

        validated = validate(arguments, tool.input_schema, "arguments")
        return tool.handler(**validated).to_payload()


def validate(value: Any, schema: dict, path: str) -> Any:
    """Validates `value` against `schema` and fills in declared defaults.

    Raises `JsonRpcError` with code -32602 on the first violation found. Supports the
    keywords used by this server's schemas: type, properties, required,
    additionalProperties, enum, minimum, maximum, minLength, maxLength, items, minItems,
    maxItems and default.
    """
    expected = schema.get("type")
    if expected is not None:
        python_type = _TYPE_NAMES[expected]
        # In Python `True` is an instance of `int`, which would let a boolean pass as a
        # number; the check below rejects that.
        wrong_type = not isinstance(value, python_type)
        if expected in ("number", "integer") and isinstance(value, bool):
            wrong_type = True
        if wrong_type:
            raise JsonRpcError(INVALID_PARAMS, f"{path}: expected {expected}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(option) for option in schema["enum"])
        raise JsonRpcError(INVALID_PARAMS, f"{path}: must be one of {allowed}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise JsonRpcError(
                INVALID_PARAMS, f"{path}: shorter than {schema['minLength']} characters"
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise JsonRpcError(
                INVALID_PARAMS, f"{path}: longer than {schema['maxLength']} characters"
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise JsonRpcError(INVALID_PARAMS, f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise JsonRpcError(INVALID_PARAMS, f"{path}: above maximum {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise JsonRpcError(INVALID_PARAMS, f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise JsonRpcError(INVALID_PARAMS, f"{path}: allows at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            value = [
                validate(item, item_schema, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]

    if isinstance(value, dict) and schema.get("type") == "object":
        value = _validate_object(value, schema, path)

    return value


def _validate_object(value: dict, schema: dict, path: str) -> dict:
    properties: dict[str, dict] = schema.get("properties", {})

    for name in schema.get("required", []):
        if name not in value:
            raise JsonRpcError(INVALID_PARAMS, f"{path}: missing required field '{name}'")

    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise JsonRpcError(
                INVALID_PARAMS, f"{path}: unknown field(s) {', '.join(unknown)}"
            )

    validated = {}
    for name, subschema in properties.items():
        if name in value:
            validated[name] = validate(value[name], subschema, f"{path}.{name}")
        elif "default" in subschema:
            validated[name] = subschema["default"]

    # Fields outside `properties` are kept when additionalProperties is not forbidden.
    for name, item in value.items():
        validated.setdefault(name, item)

    return validated
