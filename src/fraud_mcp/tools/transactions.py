"""Tool: list_transactions."""

from __future__ import annotations

from fraud_mcp.domain.models import Transaction
from fraud_mcp.infra import db
from fraud_mcp.tools.base import ToolContext, guard, load_account
from mcp_core.registry import Tool, ToolResult

DEFAULT_DAYS = 7
DEFAULT_LIMIT = 50

DESCRIPTION = (
    "List recent transactions of an account, newest first. Use it to spot candidates worth "
    "scoring; it reports what happened, not how risky it is. Filter by amount or channel to "
    "narrow a large history."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "account_id": {
            "type": "string",
            "description": "Four digit account number, for example '4471'.",
            "minLength": 1,
            "maxLength": 16,
        },
        "days": {
            "type": "integer",
            "description": "How many days back to look.",
            "minimum": 1,
            "maximum": 365,
            "default": DEFAULT_DAYS,
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of transactions to return.",
            "minimum": 1,
            "maximum": 200,
            "default": DEFAULT_LIMIT,
        },
        "min_amount": {
            "type": "number",
            "description": "Only transactions of at least this amount, in USD.",
            "minimum": 0,
        },
        "channel": {
            "type": "string",
            "description": "Restrict to one channel.",
            "enum": ["online", "in_person"],
        },
    },
    "required": ["account_id"],
    "additionalProperties": False,
}


def tools(context: ToolContext) -> list[Tool]:
    @guard
    def handler(
        account_id: str,
        days: int = DEFAULT_DAYS,
        limit: int = DEFAULT_LIMIT,
        min_amount: float | None = None,
        channel: str | None = None,
    ) -> ToolResult:
        load_account(context, account_id)  # refuses unknown accounts before querying
        since = context.since(days)

        rows = db.fetch_transactions(
            context.connection,
            account_id=account_id,
            since=since,
            limit=limit,
            min_amount=min_amount,
            channel=channel,
        )
        transactions = [Transaction.from_row(row) for row in rows]

        filters = {"days": days, "limit": limit}
        if min_amount is not None:
            filters["min_amount"] = min_amount
        if channel is not None:
            filters["channel"] = channel

        data = {
            "account_id": account_id,
            "filters": filters,
            "since": since,
            "count": len(transactions),
            "transactions": [transaction.summary() for transaction in transactions],
        }

        if not transactions:
            return ToolResult(
                text=f"No transactions for account {account_id} since {since}.", data=data
            )

        lines = [
            f"{len(transactions)} transaction(s) for account {account_id} since {since}:"
        ]
        lines += [
            f"  {transaction.txn_id}  {transaction.ts:%Y-%m-%d %H:%M}  "
            f"{transaction.amount:>9,.2f} USD  {transaction.merchant} "
            f"[{transaction.category}, {transaction.channel}, {transaction.country}]"
            for transaction in transactions
        ]
        lines.append("Score any of them with score_transaction to obtain a risk verdict.")

        return ToolResult(text="\n".join(lines), data=data)

    return [
        Tool(
            name="list_transactions",
            title="Recent transactions",
            description=DESCRIPTION,
            input_schema=SCHEMA,
            handler=handler,
        )
    ]
