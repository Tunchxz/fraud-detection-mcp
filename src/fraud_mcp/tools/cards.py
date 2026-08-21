"""Tool: freeze_card.

Freezing is the one action here that a customer feels immediately, so it takes two calls: the
first returns a confirmation token, the second applies it. The check lives in
`domain/policies.py` and runs before anything is written, which is what makes it impossible
for the model to skip it however the request is phrased.
"""

from __future__ import annotations

from datetime import timedelta

from fraud_mcp.domain import policies
from fraud_mcp.domain.models import TIMESTAMP
from fraud_mcp.infra import db
from fraud_mcp.tools.base import ToolContext, guard, load_account
from mcp_core.registry import Tool, ToolResult

DESCRIPTION = (
    "Freeze the card of an account so no new charges go through. This is a two step "
    "operation: call it with a reason to obtain a confirmation token, show that token to the "
    "analyst, and call it again with the token to apply the freeze. The reason must be "
    "identical in both calls, and the token expires in "
    f"{policies.FREEZE_TOKEN_TTL_MINUTES} minutes."
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
        "reason": {
            "type": "string",
            "description": (
                "Why the card must be frozen. Cite the evidence, for example the "
                "transaction identifier and the rule that fired."
            ),
            "minLength": 10,
            "maxLength": 500,
        },
        "confirmation_token": {
            "type": "string",
            "description": "Token returned by the first call. Omit it to request one.",
            "minLength": 4,
            "maxLength": 32,
        },
    },
    "required": ["account_id", "reason"],
    "additionalProperties": False,
}


def tools(context: ToolContext) -> list[Tool]:
    @guard
    def handler(account_id: str, reason: str, confirmation_token: str | None = None) -> ToolResult:
        _account, card = load_account(context, account_id)
        policies.check_card_active(card.status, account_id)

        now = context.now()

        if confirmation_token is None:
            token = policies.new_freeze_token()
            db.store_freeze_token(
                context.connection, token, card.card_id, reason, now.strftime(TIMESTAMP)
            )
            expires_at = now + timedelta(minutes=policies.FREEZE_TOKEN_TTL_MINUTES)

            # Reported as an error on purpose: the operation did not happen, and the model
            # must come back with the token instead of assuming success.
            return ToolResult(
                text=(
                    f"Freezing the card ending {card.last4} of account {account_id} needs "
                    f"confirmation. Reason on record: \"{reason}\". Call freeze_card again "
                    f"with confirmation_token=\"{token}\" and the same reason before "
                    f"{expires_at:%H:%M:%S} to apply it."
                ),
                data={
                    "status": "confirmation_required",
                    "account_id": account_id,
                    "card_last4": card.last4,
                    "reason": reason,
                    "confirmation_token": token,
                    "expires_at": expires_at.strftime(TIMESTAMP),
                    "valid_for_minutes": policies.FREEZE_TOKEN_TTL_MINUTES,
                },
                is_error=True,
            )

        token_row = db.fetch_freeze_token(context.connection, confirmation_token)
        policies.check_freeze_token(token_row, confirmation_token, card.card_id, reason, now)

        stamp = now.strftime(TIMESTAMP)
        db.freeze_card(context.connection, card.card_id, reason, stamp)
        db.consume_freeze_token(context.connection, confirmation_token, stamp)

        return ToolResult(
            text=(
                f"Card ending {card.last4} of account {account_id} is now frozen as of "
                f"{stamp}. Reason: {reason}. No new charges will be authorised."
            ),
            data={
                "status": "frozen",
                "account_id": account_id,
                "card_id": card.card_id,
                "card_last4": card.last4,
                "frozen_at": stamp,
                "reason": reason,
            },
        )

    return [
        Tool(
            name="freeze_card",
            title="Freeze card",
            description=DESCRIPTION,
            input_schema=SCHEMA,
            handler=handler,
        )
    ]
