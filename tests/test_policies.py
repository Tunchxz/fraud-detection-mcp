"""The deterministic controls.

These are the checks the model must not be able to talk its way around (the confirmation
token for a freeze and the conditions for a dispute).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fraud_mcp.domain import policies
from fraud_mcp.domain.errors import DomainError
from fraud_mcp.domain.models import TIMESTAMP, Transaction

NOW = datetime(2026, 5, 20, 13, 0, 0)
CARD = "card_4471"
REASON = "Impossible geovelocity on txn_000001"


def token_row(
    token: str = "ABCD1234",
    card_id: str = CARD,
    reason: str = REASON,
    created_at: datetime = NOW,
    used_at: str | None = None,
) -> dict:
    return {
        "token": token,
        "card_id": card_id,
        "reason": reason,
        "created_at": created_at.strftime(TIMESTAMP),
        "used_at": used_at,
    }


def transaction(ts: datetime = NOW, txn_id: str = "txn_000001") -> Transaction:
    return Transaction(
        txn_id=txn_id,
        account_id="4471",
        card_id=CARD,
        ts=ts,
        amount=318.75,
        merchant="Test Merchant",
        category="gas_transport",
        channel="in_person",
        country="GT",
        merch_lat=14.6349,
        merch_lon=-90.5069,
    )


# --- Confirmation token ------------------------------------------------------------


def test_a_fresh_token_is_accepted():
    policies.check_freeze_token(token_row(), "ABCD1234", CARD, REASON, NOW + timedelta(minutes=1))


def test_tokens_are_unguessable_and_unique():
    tokens = {policies.new_freeze_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(token) == 8 for token in tokens)


def test_an_unknown_token_is_refused():
    with pytest.raises(DomainError, match="unknown"):
        policies.check_freeze_token(None, "NOPE", CARD, REASON, NOW)


def test_a_used_token_is_refused():
    used = token_row(used_at=NOW.strftime(TIMESTAMP))
    with pytest.raises(DomainError, match="already used"):
        policies.check_freeze_token(used, "ABCD1234", CARD, REASON, NOW)


def test_a_token_from_another_card_is_refused():
    with pytest.raises(DomainError, match="another card"):
        policies.check_freeze_token(token_row(card_id="card_4472"), "ABCD1234", CARD, REASON, NOW)


def test_changing_the_reason_invalidates_the_token():
    # The reason is what the analyst approved, so it must survive to the second call.
    with pytest.raises(DomainError, match="reason does not match"):
        policies.check_freeze_token(token_row(), "ABCD1234", CARD, "Something else entirely", NOW)


def test_an_expired_token_is_refused():
    late = NOW + timedelta(minutes=policies.FREEZE_TOKEN_TTL_MINUTES + 1)
    with pytest.raises(DomainError, match="expired"):
        policies.check_freeze_token(token_row(), "ABCD1234", CARD, REASON, late)


def test_a_token_at_the_edge_of_its_window_is_still_valid():
    edge = NOW + timedelta(minutes=policies.FREEZE_TOKEN_TTL_MINUTES)
    policies.check_freeze_token(token_row(), "ABCD1234", CARD, REASON, edge)


def test_freezing_a_frozen_card_is_refused():
    with pytest.raises(DomainError, match="already frozen"):
        policies.check_card_active("frozen", "4471")


def test_freezing_an_active_card_is_allowed():
    policies.check_card_active("active", "4471")


# --- Dispute conditions ------------------------------------------------------------


def test_a_recent_transaction_is_inside_the_claim_window():
    policies.check_dispute_window(transaction(ts=NOW - timedelta(days=3)), NOW)


def test_a_transaction_at_the_edge_of_the_window_is_accepted():
    edge = NOW - timedelta(days=policies.DISPUTE_WINDOW_DAYS)
    policies.check_dispute_window(transaction(ts=edge), NOW)


def test_a_stale_transaction_is_refused():
    stale = NOW - timedelta(days=policies.DISPUTE_WINDOW_DAYS + 1)
    with pytest.raises(DomainError, match="claim window"):
        policies.check_dispute_window(transaction(ts=stale), NOW)


@pytest.mark.parametrize("level", ["high", "medium"])
def test_risky_transactions_can_be_disputed(level):
    policies.check_dispute_risk(transaction(), level)


def test_a_low_risk_transaction_cannot_be_disputed():
    with pytest.raises(DomainError, match="does not support a dispute"):
        policies.check_dispute_risk(transaction(), "low")


def test_a_second_dispute_over_the_same_transaction_is_refused():
    existing = {"case_id": "CASE-0001", "dispute_id": "DSP-0001"}
    with pytest.raises(DomainError, match="already disputed"):
        policies.check_not_disputed(transaction(), existing)


def test_an_undisputed_transaction_passes():
    policies.check_not_disputed(transaction(), None)
