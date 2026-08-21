"""Tool: score_transaction."""

from __future__ import annotations

from fraud_mcp.domain import risk
from fraud_mcp.tools.base import ToolContext, guard, load_account, load_history, load_transaction
from mcp_core.registry import Tool, ToolResult

DESCRIPTION = (
    "Assess one transaction. Returns the model's risk score together with the explicit rules "
    "it violates, each with the evidence behind it. The rules are what can be explained to a "
    "customer or an auditor, so quote them when reporting a decision."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "transaction_id": {
            "type": "string",
            "description": "Identifier returned by list_transactions, for example 'txn_053503'.",
            "minLength": 1,
            "maxLength": 64,
        }
    },
    "required": ["transaction_id"],
    "additionalProperties": False,
}


def tools(context: ToolContext) -> list[Tool]:
    @guard
    def handler(transaction_id: str) -> ToolResult:
        transaction = load_transaction(context, transaction_id)
        account, _card = load_account(context, transaction.account_id)
        history = load_history(context, transaction)

        verdict = risk.assess(transaction, account, history, context.model)

        data = {
            "transaction": transaction.summary(),
            "account_id": account.account_id,
            **verdict.to_dict(),
            "card_history": {
                "prior_transactions": history.prior_count,
                "average_amount": (
                    round(history.prior_mean, 2) if history.prior_mean else None
                ),
                "previous_transaction_id": (
                    history.previous.txn_id if history.previous else None
                ),
            },
            "model_trained_at": context.model.trained_at,
        }

        lines = [
            f"{transaction.txn_id} — {transaction.amount:,.2f} USD at {transaction.merchant} "
            f"[{transaction.category}, {transaction.channel}, {transaction.country}] on "
            f"{transaction.ts:%Y-%m-%d %H:%M}.",
            f"Risk: {verdict.level.upper()} (score {verdict.score:.4f}, threshold "
            f"{verdict.threshold:.2f}"
            + (" exceeded" if verdict.exceeds_threshold else " not reached")
            + ").",
        ]

        if verdict.hits:
            lines.append(f"{len(verdict.hits)} rule(s) triggered:")
            lines += [f"  [{hit.severity}] {hit.rule}: {hit.reason}" for hit in verdict.hits]
        else:
            lines.append("No explicit rule was triggered.")

        if verdict.drivers:
            drivers = ", ".join(f"{name} (+{value})" for name, value in verdict.drivers)
            lines.append(f"Model variables pushing the score up: {drivers}.")

        return ToolResult(text="\n".join(lines), data=data)

    return [
        Tool(
            name="score_transaction",
            title="Transaction risk assessment",
            description=DESCRIPTION,
            input_schema=SCHEMA,
            handler=handler,
        )
    ]
