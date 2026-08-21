"""Shared plumbing for the tools.

The tools are adapters, they validate what the model sent, load entities from the database,
call the domain, and shape the answer. No business rule lives here.

`guard` is what keeps the two error worlds apart. A `DomainError` (a refusal by the rules)
comes back as a result with `isError: true`, as the MCP specification prescribes, while a
malformed request keeps travelling as a JSON-RPC error raised further down.
"""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from fraud_mcp.domain.errors import DomainError
from fraud_mcp.domain.model import RiskModel
from fraud_mcp.domain.models import TIMESTAMP, Account, Card, CardHistory, Transaction
from fraud_mcp.infra import db
from mcp_core.registry import ToolResult

# Window used to describe an account's habits.
PROFILE_WINDOW_DAYS = 90


@dataclass
class ToolContext:
    """Everything the tools need: the database, the model and the clock."""

    connection: sqlite3.Connection
    model: RiskModel

    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)

    def stamp(self) -> str:
        return self.now().strftime(TIMESTAMP)

    def since(self, days: int) -> str:
        return (self.now() - timedelta(days=days)).strftime(TIMESTAMP)


def guard(handler: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
    """Turns a domain refusal into a tool execution error the model can act on."""

    @functools.wraps(handler)
    def wrapper(**arguments) -> ToolResult:
        try:
            return handler(**arguments)
        except DomainError as error:
            return ToolResult(
                text=error.to_text(),
                data={"error": error.message, **error.details},
                is_error=True,
            )

    return wrapper


def load_account(context: ToolContext, account_id: str) -> tuple[Account, Card]:
    """Loads an account and its card, or refuses with the list of known accounts."""
    account_row = db.fetch_account(context.connection, account_id)
    if account_row is None:
        known = context.connection.execute(
            "SELECT account_id FROM accounts ORDER BY account_id"
        ).fetchall()
        raise DomainError(
            f"Account {account_id} does not exist.",
            hint=f"Known accounts: {', '.join(row['account_id'] for row in known)}.",
            account_id=account_id,
        )

    card_row = db.fetch_card(context.connection, account_id)
    if card_row is None:
        raise DomainError(f"Account {account_id} has no card on file.", account_id=account_id)

    return Account.from_row(account_row), Card.from_row(card_row)


def load_transaction(context: ToolContext, transaction_id: str) -> Transaction:
    row = db.fetch_transaction(context.connection, transaction_id)
    if row is None:
        raise DomainError(
            f"Transaction {transaction_id} does not exist.",
            hint="Use list_transactions to obtain valid transaction identifiers.",
            transaction_id=transaction_id,
        )
    return Transaction.from_row(row)


def load_history(context: ToolContext, transaction: Transaction) -> CardHistory:
    """Loads what the card did before this transaction, and nothing after it."""
    timestamp = transaction.ts.strftime(TIMESTAMP)
    prior_count, prior_mean = db.prior_amount_stats(
        context.connection, transaction.card_id, timestamp
    )
    previous_row = db.previous_transaction(context.connection, transaction.card_id, timestamp)

    return CardHistory(
        prior_count=prior_count,
        prior_mean=prior_mean,
        previous=Transaction.from_row(previous_row) if previous_row is not None else None,
    )
