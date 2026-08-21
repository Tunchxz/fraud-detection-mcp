"""Tool: get_account."""

from __future__ import annotations

from fraud_mcp.infra import db
from fraud_mcp.tools.base import PROFILE_WINDOW_DAYS, ToolContext, guard, load_account
from mcp_core.registry import Tool, ToolResult

DESCRIPTION = (
    "Look up a monitored account: cardholder, card status and spending habits. Start an "
    "investigation here to learn what is normal for this customer before judging any "
    "single transaction."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "account_id": {
            "type": "string",
            "description": "Four digit account number, for example '4471'.",
            "minLength": 1,
            "maxLength": 16,
        }
    },
    "required": ["account_id"],
    "additionalProperties": False,
}


def tools(context: ToolContext) -> list[Tool]:
    @guard
    def handler(account_id: str) -> ToolResult:
        account, card = load_account(context, account_id)
        since = context.since(PROFILE_WINDOW_DAYS)

        profile = db.spending_profile(context.connection, account_id, since)
        categories = db.top_categories(context.connection, account_id, since)
        open_cases = db.count_open_cases(context.connection, account_id)

        data = {
            "account_id": account.account_id,
            "holder_name": account.holder_name,
            "location": f"{account.city}, {account.state}",
            "occupation": account.job,
            "card": {
                "card_id": card.card_id,
                "last4": card.last4,
                "status": card.status,
                "frozen_at": card.frozen_at,
                "freeze_reason": card.freeze_reason,
            },
            "spending_profile": {
                "window_days": PROFILE_WINDOW_DAYS,
                "transactions": int(profile["transactions"] or 0),
                "average_amount": round(profile["average_amount"] or 0.0, 2),
                "largest_amount": round(profile["largest_amount"] or 0.0, 2),
                "total_amount": round(profile["total_amount"] or 0.0, 2),
                "top_categories": [
                    {
                        "category": row["category"],
                        "transactions": row["transactions"],
                        "total_amount": round(row["total_amount"], 2),
                    }
                    for row in categories
                ],
            },
            "open_cases": open_cases,
        }

        top = ", ".join(row["category"] for row in categories) or "none"
        text = (
            f"Account {account.account_id} — {account.holder_name} "
            f"({account.city}, {account.state}).\n"
            f"Card ending {card.last4}: {card.status}"
            + (f" since {card.frozen_at} ({card.freeze_reason})" if card.is_frozen else "")
            + ".\n"
            f"Last {PROFILE_WINDOW_DAYS} days: {data['spending_profile']['transactions']} "
            f"transactions, {data['spending_profile']['average_amount']:,.2f} USD average, "
            f"{data['spending_profile']['largest_amount']:,.2f} USD largest.\n"
            f"Most used categories: {top}.\n"
            f"Open cases: {open_cases}."
        )
        return ToolResult(text=text, data=data)

    return [
        Tool(
            name="get_account",
            title="Account overview",
            description=DESCRIPTION,
            input_schema=SCHEMA,
            handler=handler,
        )
    ]
