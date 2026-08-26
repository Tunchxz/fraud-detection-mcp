"""Building the database from the versioned seed extract."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fraud_mcp.domain import rules
from fraud_mcp.domain.models import Account, CardHistory, Transaction
from fraud_mcp.infra import seed

from conftest import needs_seed

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = needs_seed


@pytest.fixture(scope="module")
def built(seeded) -> tuple[seed.SeedSummary, sqlite3.Connection]:
    connection = sqlite3.connect(seeded.database)
    connection.row_factory = sqlite3.Row
    yield seeded, connection
    connection.close()


def test_it_builds_the_expected_shape(built):
    summary, connection = built

    assert summary.accounts == 20
    assert summary.transactions > 50_000

    counted = connection.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    assert counted == summary.transactions

    identifiers = [
        row["account_id"] for row in connection.execute("SELECT account_id FROM accounts ORDER BY account_id")
    ]
    assert identifiers[0] == "4471" and identifiers[-1] == "4490"


def test_the_dates_are_moved_to_the_present(built):
    summary, connection = built
    newest = datetime.strptime(summary.last_ts, seed.TIMESTAMP)

    # The dataset ends in 2020; without the shift nothing would fall inside a recent window.
    assert datetime.now() - newest < timedelta(days=1)
    assert summary.shift_days > 1_000

    recent = connection.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE ts >= datetime('now', '-7 days')"
    ).fetchone()["n"]
    assert recent > 0


def test_ages_are_preserved_by_shifting_the_birth_dates(built):
    """The model was trained on the cardholders' ages, so they must not drift."""
    _summary, connection = built
    row = connection.execute(
        "SELECT a.dob, MAX(t.ts) AS last_ts FROM accounts a "
        "JOIN transactions t ON t.account_id = a.account_id GROUP BY a.account_id LIMIT 1"
    ).fetchone()

    born = datetime.strptime(row["dob"], seed.DATE)
    age = (datetime.strptime(row["last_ts"], seed.TIMESTAMP) - born).days / 365.25
    assert 15 < age < 100


def test_the_demonstration_case_triggers_the_geovelocity_rule(built):
    summary, connection = built
    rows = connection.execute(
        "SELECT * FROM transactions WHERE account_id = ? ORDER BY ts", (summary.demo_account,)
    ).fetchall()

    abroad = next(row for row in rows if row["country"] == "GT")
    previous = connection.execute(
        "SELECT * FROM transactions WHERE card_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
        (abroad["card_id"], abroad["ts"]),
    ).fetchone()

    account = Account.from_row(
        connection.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (summary.demo_account,)
        ).fetchone()
    )
    history = CardHistory(prior_count=100, prior_mean=80.0, previous=Transaction.from_row(previous))
    hits = rules.evaluate(Transaction.from_row(abroad), account, history)

    hit = next(item for item in hits if item.rule == "impossible_geovelocity")
    assert hit.evidence["implied_speed_kmh"] > rules.IMPOSSIBLE_SPEED_KMH
    assert hit.evidence["distance_km"] > rules.IMPOSSIBLE_DISTANCE_KM


def test_the_geovelocity_rule_stays_quiet_on_the_rest_of_the_data(built):
    """The rule must point at the injected jump, not at the generator's noise.

    Every consecutive card-present pair in the database is walked. Nothing that comes purely
    from the dataset may fire.

    Pairs that involve an injected transaction are counted apart on purpose. There are two of
    them, and both are the demonstration: the trip to Guatemala, and the return, which is the
    cardholder using their own card at home while the clone is still abroad. How much later
    that return happens depends on the moment of seeding, so only the outbound jump is
    asserted.
    """
    summary, connection = built
    injected = {row[0] for row in summary.demo_transactions}
    rows = connection.execute(
        "SELECT * FROM transactions WHERE channel = 'in_person' ORDER BY card_id, ts"
    ).fetchall()

    from_the_dataset, from_the_demo = [], []
    previous = None
    for row in rows:
        if previous is not None and previous["card_id"] == row["card_id"]:
            current, earlier = Transaction.from_row(row), Transaction.from_row(previous)
            speed = rules.implied_speed_kmh(current, earlier)
            distance = rules.haversine_km(
                earlier.merch_lat, earlier.merch_lon, current.merch_lat, current.merch_lon
            )
            if speed and speed > rules.IMPOSSIBLE_SPEED_KMH and distance > rules.IMPOSSIBLE_DISTANCE_KM:
                pair = (earlier.txn_id, current.txn_id)
                if injected.isdisjoint(pair):
                    from_the_dataset.append(pair)
                else:
                    from_the_demo.append(pair)
        previous = row

    assert from_the_dataset == [], f"the rule fired on the dataset itself: {from_the_dataset[:5]}"

    abroad = connection.execute(
        "SELECT txn_id FROM transactions WHERE country = 'GT'"
    ).fetchone()["txn_id"]
    assert any(abroad in pair for pair in from_the_demo), "the injected jump must fire"


def test_the_preseeded_state_is_in_place(built):
    summary, connection = built

    frozen = connection.execute(
        "SELECT * FROM cards WHERE account_id = ?", (summary.frozen_account,)
    ).fetchone()
    assert frozen["status"] == "frozen"
    assert frozen["freeze_reason"]

    case = connection.execute(
        "SELECT * FROM cases WHERE case_id = ?", (summary.case_id,)
    ).fetchone()
    assert case["account_id"] == summary.case_account
    assert case["status"] == "open"

    dispute = connection.execute(
        "SELECT * FROM disputes WHERE case_id = ?", (summary.case_id,)
    ).fetchone()
    assert dispute["txn_id"] == summary.disputed_transaction

    # The demonstration account is left clean, so the whole scenario can be walked on it.
    demo_card = connection.execute(
        "SELECT * FROM cards WHERE account_id = ?", (summary.demo_account,)
    ).fetchone()
    assert demo_card["status"] == "active"


def test_ensure_database_builds_only_when_missing(tmp_path: Path):
    database = tmp_path / "fraud.db"

    assert seed.ensure_database(database) is not None
    assert database.exists()

    stamp = database.stat().st_mtime_ns
    assert seed.ensure_database(database) is None, "an existing database must not be rebuilt"
    assert database.stat().st_mtime_ns == stamp
