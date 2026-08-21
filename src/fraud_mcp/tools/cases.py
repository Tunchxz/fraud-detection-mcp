"""Tools: open_dispute and get_case_status.

A dispute is only accepted when the transaction is inside the claim window, was assessed as
medium or high risk, and is not already disputed. Those checks are deterministic and live in
`domain/policies.py`; transactions that fail one are reported back with the reason instead of
being silently dropped.
"""

from __future__ import annotations

from fraud_mcp.domain import policies, risk
from fraud_mcp.domain.errors import DomainError
from fraud_mcp.domain.models import TIMESTAMP
from fraud_mcp.infra import db
from fraud_mcp.tools.base import ToolContext, guard, load_account, load_history, load_transaction
from mcp_core.registry import Tool, ToolResult

OPEN_DISPUTE_DESCRIPTION = (
    "Open a chargeback claim over one or more transactions of the same account. Each "
    "transaction is checked independently: it must be within the "
    f"{policies.DISPUTE_WINDOW_DAYS} day claim window, assessed as medium or high risk, and "
    "not already disputed. Accepted transactions are grouped into a case; rejected ones come "
    "back with the reason."
)

OPEN_DISPUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "transaction_ids": {
            "type": "array",
            "description": "Transactions to dispute. They must belong to the same account.",
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
            "minItems": 1,
            "maxItems": 20,
        },
        "reason": {
            "type": "string",
            "description": "Why the charges are being claimed.",
            "minLength": 10,
            "maxLength": 500,
        },
    },
    "required": ["transaction_ids", "reason"],
    "additionalProperties": False,
}

CASE_STATUS_DESCRIPTION = (
    "Check a fraud case: its status, when it was opened, the amount under dispute and the "
    "transactions it covers."
)

CASE_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "case_id": {
            "type": "string",
            "description": "Case identifier, for example 'CASE-0001'.",
            "minLength": 1,
            "maxLength": 32,
        }
    },
    "required": ["case_id"],
    "additionalProperties": False,
}


def tools(context: ToolContext) -> list[Tool]:
    @guard
    def open_dispute(transaction_ids: list[str], reason: str) -> ToolResult:
        now = context.now()
        transactions = [load_transaction(context, txn_id) for txn_id in transaction_ids]

        accounts = {transaction.account_id for transaction in transactions}
        if len(accounts) > 1:
            raise DomainError(
                "The transactions belong to different accounts: " + ", ".join(sorted(accounts)),
                hint="Open one dispute per account.",
            )

        account_id = transactions[0].account_id
        account, _card = load_account(context, account_id)

        accepted, rejected = [], []
        for transaction in transactions:
            try:
                policies.check_dispute_window(transaction, now)
                policies.check_not_disputed(
                    transaction, db.dispute_for_transaction(context.connection, transaction.txn_id)
                )
                history = load_history(context, transaction)
                verdict = risk.assess(transaction, account, history, context.model)
                policies.check_dispute_risk(transaction, verdict.level)
            except DomainError as refusal:
                rejected.append({
                    "transaction_id": transaction.txn_id,
                    "reason": refusal.to_text(),
                })
                continue

            accepted.append((transaction, verdict))

        if not accepted:
            return ToolResult(
                text=(
                    "No dispute was opened: every transaction was refused.\n"
                    + "\n".join(f"  {item['transaction_id']}: {item['reason']}" for item in rejected)
                ),
                data={"status": "rejected", "accepted": [], "rejected": rejected},
                is_error=True,
            )

        stamp = now.strftime(TIMESTAMP)
        case = db.open_case_for_account(context.connection, account_id)
        if case is None:
            case_id = f"CASE-{db.next_sequence(context.connection, 'cases'):04d}"
            db.insert_case(context.connection, case_id, account_id, reason, stamp)
        else:
            case_id = case["case_id"]

        sequence = db.next_sequence(context.connection, "disputes")
        disputed = []
        for offset, (transaction, verdict) in enumerate(accepted):
            dispute_id = f"DSP-{sequence + offset:04d}"
            db.insert_dispute(
                context.connection,
                dispute_id,
                case_id,
                transaction.txn_id,
                transaction.amount,
                reason,
                stamp,
            )
            disputed.append({
                "dispute_id": dispute_id,
                "transaction_id": transaction.txn_id,
                "amount": round(transaction.amount, 2),
                "risk_level": verdict.level,
                "risk_score": round(verdict.score, 4),
            })

        total = round(sum(item["amount"] for item in disputed), 2)
        lines = [
            f"Case {case_id} for account {account_id}: {len(disputed)} dispute(s) opened for "
            f"{total:,.2f} USD."
        ]
        lines += [
            f"  {item['dispute_id']}  {item['transaction_id']}  {item['amount']:,.2f} USD  "
            f"(risk {item['risk_level']}, score {item['risk_score']})"
            for item in disputed
        ]
        if rejected:
            lines.append(f"{len(rejected)} transaction(s) refused:")
            lines += [f"  {item['transaction_id']}: {item['reason']}" for item in rejected]

        return ToolResult(
            text="\n".join(lines),
            data={
                "status": "open",
                "case_id": case_id,
                "account_id": account_id,
                "opened_at": stamp,
                "disputed_amount": total,
                "accepted": disputed,
                "rejected": rejected,
            },
        )

    @guard
    def get_case_status(case_id: str) -> ToolResult:
        case = db.fetch_case(context.connection, case_id)
        if case is None:
            raise DomainError(
                f"Case {case_id} does not exist.",
                hint="Case identifiers look like CASE-0001 and are returned by open_dispute.",
                case_id=case_id,
            )

        disputes = db.fetch_case_disputes(context.connection, case_id)
        total = round(sum(row["amount"] for row in disputes), 2)

        data = {
            "case_id": case["case_id"],
            "account_id": case["account_id"],
            "status": case["status"],
            "opened_at": case["opened_at"],
            "reason": case["reason"],
            "disputed_amount": total,
            "disputes": [
                {
                    "dispute_id": row["dispute_id"],
                    "transaction_id": row["txn_id"],
                    "merchant": row["merchant"],
                    "transaction_timestamp": row["transaction_ts"],
                    "amount": round(row["amount"], 2),
                    "status": row["status"],
                }
                for row in disputes
            ],
        }

        lines = [
            f"Case {case['case_id']} — account {case['account_id']}, status {case['status']}, "
            f"opened {case['opened_at']}.",
            f"Amount under dispute: {total:,.2f} USD across {len(disputes)} transaction(s).",
            f"Reason on record: {case['reason']}",
        ]
        lines += [
            f"  {item['dispute_id']}  {item['transaction_id']}  {item['amount']:,.2f} USD  "
            f"{item['merchant']}  [{item['status']}]"
            for item in data["disputes"]
        ]

        return ToolResult(text="\n".join(lines), data=data)

    return [
        Tool(
            name="open_dispute",
            title="Open dispute",
            description=OPEN_DISPUTE_DESCRIPTION,
            input_schema=OPEN_DISPUTE_SCHEMA,
            handler=open_dispute,
        ),
        Tool(
            name="get_case_status",
            title="Case status",
            description=CASE_STATUS_DESCRIPTION,
            input_schema=CASE_STATUS_SCHEMA,
            handler=get_case_status,
        ),
    ]
