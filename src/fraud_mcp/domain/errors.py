"""Domain errors.

A `DomainError` is a refusal by the business rules (an unknown account, a missing
confirmation token, a dispute past its deadline) and not a protocol failure. The tool
layer turns it into a result with `isError: true`, which is what the MCP specification
prescribes so the model can read the reason and correct itself.

Protocol failures (unknown method, invalid arguments) travel as JSON-RPC errors instead
and are raised by `mcp_core`.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    def __init__(self, message: str, hint: str = "", **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_text(self) -> str:
        return f"{self.message} {self.hint}".strip()
