"""Deterministic controls that the language model cannot skip.

These are checks from the use-case proposal: freezing a card takes a confirmation token,
and a dispute is refused outside the claim window or when the transaction is not risky
enough. They live here, in the domain, and the tools call them before touching anything
(so no phrasing of a prompt can talk the server out of them).

Every refusal raises `DomainError`, which the tool layer reports as `isError: true` with
the reason spelled out, so the model can react instead of guessing.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from fraud_mcp.domain.errors import DomainError
from fraud_mcp.domain.models import Transaction, parse_timestamp
from fraud_mcp.domain.risk import HIGH, MEDIUM

# A token is short-lived (it confirms a decision, not a standing permission).
FREEZE_TOKEN_TTL_MINUTES = 10

# Card scheme chargeback windows sit at 120 days from the transaction date.
DISPUTE_WINDOW_DAYS = 120

# A dispute needs evidence behind it; `low` risk is not evidence.
DISPUTABLE_RISK_LEVELS = (HIGH, MEDIUM)


def new_freeze_token() -> str:
    """A short, unguessable token the analyst repeats back to confirm the freeze."""
    return secrets.token_hex(4).upper()


def check_freeze_token(
    token_row: Mapping[str, Any] | None,
    token: str,
    card_id: str,
    reason: str,
    now: datetime,
) -> None:
    """Validates a confirmation token, or refuses the freeze.

    The token must exist, be unused, belong to this card, carry the same reason it was
    issued for, and still be inside its validity window.
    """
    if token_row is None:
        raise DomainError(
            f"Confirmation token '{token}' is unknown.",
            hint="Call freeze_card without a token to obtain a fresh one.",
            token=token,
        )

    if token_row["used_at"] is not None:
        raise DomainError(
            f"Confirmation token '{token}' was already used at {token_row['used_at']}.",
            hint="Request a new token if the card must be frozen again.",
        )

    if token_row["card_id"] != card_id:
        raise DomainError(
            f"Confirmation token '{token}' belongs to another card.",
            hint="Tokens are bound to the card they were issued for.",
        )

    if token_row["reason"] != reason:
        raise DomainError(
            "The reason does not match the one the token was issued for: "
            f"'{token_row['reason']}'.",
            hint="Repeat the original reason, or request a new token.",
        )

    issued_at = parse_timestamp(token_row["created_at"])
    if now - issued_at > timedelta(minutes=FREEZE_TOKEN_TTL_MINUTES):
        raise DomainError(
            f"Confirmation token '{token}' expired: it was issued at "
            f"{token_row['created_at']} and is valid for {FREEZE_TOKEN_TTL_MINUTES} minutes.",
            hint="Call freeze_card without a token to obtain a fresh one.",
        )


def check_dispute_window(transaction: Transaction, now: datetime) -> None:
    """Refuses transactions older than the claim window."""
    age_days = (now - transaction.ts).days
    if age_days > DISPUTE_WINDOW_DAYS:
        raise DomainError(
            f"Transaction {transaction.txn_id} is {age_days} days old, past the "
            f"{DISPUTE_WINDOW_DAYS} day claim window.",
            hint="Disputes outside the window must be escalated to the issuer instead.",
            transaction_id=transaction.txn_id,
            age_days=age_days,
        )


def check_dispute_risk(transaction: Transaction, risk_level: str) -> None:
    """Refuses transactions the analysis did not find risky."""
    if risk_level not in DISPUTABLE_RISK_LEVELS:
        raise DomainError(
            f"Transaction {transaction.txn_id} was assessed as '{risk_level}' risk, which "
            "does not support a dispute.",
            hint="Score the transaction first and dispute only medium or high risk ones.",
            transaction_id=transaction.txn_id,
            risk_level=risk_level,
        )


def check_not_disputed(
    transaction: Transaction, existing: Mapping[str, Any] | None
) -> None:
    """Refuses a second dispute over the same transaction."""
    if existing is not None:
        raise DomainError(
            f"Transaction {transaction.txn_id} is already disputed under case "
            f"{existing['case_id']}.",
            hint="Check the case status instead of opening a second dispute.",
            transaction_id=transaction.txn_id,
            case_id=existing["case_id"],
        )


def check_card_active(card_status: str, account_id: str) -> None:
    """Refuses freezing a card that is already frozen."""
    if card_status == "frozen":
        raise DomainError(
            f"The card of account {account_id} is already frozen.",
            hint="No further action is needed to stop new charges.",
            account_id=account_id,
        )
