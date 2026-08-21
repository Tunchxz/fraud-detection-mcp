"""Builds the fraud monitoring server (identity + the six tools).

The result is transport-agnostic on purpose. The stdio entry point wires it to standard
input and output; the HTTP entry point will wire the very same registry to a socket, which
is what makes the local and the remote server one server rather than two.
"""

from __future__ import annotations

from fraud_mcp.domain.model import RiskModel
from fraud_mcp.infra import db
from fraud_mcp.tools import accounts, cards, cases, scoring, transactions
from fraud_mcp.tools.base import ToolContext
from mcp_core.protocol import ServerInfo
from mcp_core.registry import ToolRegistry

NAME = "fraud-monitoring"
TITLE = "Bank Fraud Monitoring"
VERSION = "0.1.0"

# Sent to the host in the initialize result. It orients the model on the intended workflow
# without constraining it, and states the two controls it cannot talk its way around.
INSTRUCTIONS = (
    "Tools for a bank fraud monitoring analyst working on simulated data. A typical "
    "investigation goes: get_account to learn what is normal for the customer, "
    "list_transactions to see recent activity, score_transaction on anything suspicious, and "
    "then freeze_card or open_dispute if the evidence supports it. Always quote the triggered "
    "rules when explaining a decision. Two controls are enforced by the server and cannot be "
    "bypassed: freezing a card takes a confirmation token, and a dispute is refused outside "
    "the claim window or on transactions that are not at least medium risk."
)

# The order tools are registered in is the order tools/list reports them, and it follows the
# investigation workflow.
TOOL_MODULES = (accounts, transactions, scoring, cards, cases)


def build_context() -> ToolContext:
    return ToolContext(connection=db.connect(), model=RiskModel.load())


def build_registry(context: ToolContext) -> ToolRegistry:
    registry = ToolRegistry()
    for module in TOOL_MODULES:
        for tool in module.tools(context):
            registry.register(tool)
    return registry


def server_info() -> ServerInfo:
    return ServerInfo(name=NAME, title=TITLE, version=VERSION, instructions=INSTRUCTIONS)
