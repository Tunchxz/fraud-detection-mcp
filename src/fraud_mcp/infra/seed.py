"""Builds the monitoring database from the versioned seed extract.

`data/seed/*.csv.gz` holds twenty accounts with their history, taken from the public dataset
by `scripts/extract_seed.py`. This module turns that extract into `data/fraud.db`.

The three main events that happen during seeding are:

- The dates are moved to the present. The dataset ends on 2020-12-31, so every timestamp
  is shifted by a single delta that lands the newest transaction on the moment of seeding.
  Dates of birth move by the same delta, so the cardholder's age stays the one the
  model saw during training.
- The demonstration case is injected, since the dataset cannot produce a purchase abroad
  minutes after a domestic one.
- Some state is pre-seeded (a frozen card and an open case) so the policies and
  `get_case_status` have material on a freshly built database.

All timestamps are naive local time.
"""

from __future__ import annotations

import csv
import gzip
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fraud_mcp.infra.db import DEFAULT_DB_PATH, create_schema, database_path

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_DIR = Path(os.environ.get("FRAUD_MCP_SEED") or REPO_ROOT / "data" / "seed")

TIMESTAMP = "%Y-%m-%d %H:%M:%S"
DATE = "%Y-%m-%d"

ACCOUNT_COLUMNS = [
    "account_id",
    "holder_name",
    "city",
    "state",
    "lat",
    "lon",
    "city_pop",
    "dob",
    "gender",
    "job",
    "opened_at",
]
CARD_COLUMNS = ["card_id", "account_id", "last4"]
TRANSACTION_COLUMNS = [
    "txn_id",
    "account_id",
    "card_id",
    "ts",
    "amount",
    "merchant",
    "category",
    "channel",
    "country",
    "merch_lat",
    "merch_lon",
    "is_fraud",
]

# Account for the demonstration scenario.
DEMO_ACCOUNT_ID = "4471"

# Accounts carrying pre-seeded state.
FROZEN_ACCOUNT_ID = "4472"
CASE_ACCOUNT_ID = "4474"

# Coordinates used by the injected case.
GUATEMALA_CITY = (14.6349, -90.5069)


def channel_of(category: str) -> str:
    """`_net` categories are online purchases; the rest are card-present."""
    return "online" if category.endswith("_net") else "in_person"


@dataclass(frozen=True)
class SeedSummary:
    """What the seeding produced, for the command line report and the tests."""

    database: Path
    accounts: int
    transactions: int
    first_ts: str
    last_ts: str
    shift_days: int
    demo_account: str
    demo_transactions: list[tuple[str, str, float, str, str]]
    frozen_account: str
    case_id: str
    case_account: str
    disputed_transaction: str


def _read(name: str) -> list[dict]:
    path = SEED_DIR / f"{name}.csv.gz"
    if not path.exists():
        raise FileNotFoundError(
            f"Seed extract not found at {path}. It is versioned with the repository; "
            "regenerate it with 'python scripts/extract_seed.py' if the dataset is available."
        )
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _demo_rows(account: dict, card_id: str, first_sequence: int, now: datetime) -> list[tuple]:
    """The case from the proposal, injected into the demonstration account.

    Three pieces: a pair of card-present purchases with an impossible geovelocity, two
    high-value online purchases at night for the model to score, and one old transaction that
    exercises the refusal by claim window.
    """
    latitude, longitude = float(account["lat"]), float(account["lon"])

    home_leg = (now - timedelta(days=6)).replace(hour=3, minute=12, second=0)
    abroad_leg = home_leg + timedelta(minutes=14)
    night = (now - timedelta(days=5)).replace(hour=23, minute=31, second=0)
    stale = (now - timedelta(days=200)).replace(hour=22, minute=10, second=0)

    rows = [
        # An ordinary card-present purchase in the cardholder's own city.
        (home_leg, 62.40, f"Kozey-Boyle Market {account['city']}", "grocery_pos", "US",
         latitude, longitude, 0),
        # Fourteen minutes later, another card-present purchase thousands of km away.
        (abroad_leg, 318.75, "Estacion Zona 10", "gas_transport", "GT", *GUATEMALA_CITY, 1),
        (night, 1180.00, "Cormier-Dickerson Online", "shopping_net", "US", latitude, longitude, 1),
        (night + timedelta(minutes=16), 940.50, "Rutherford Digital", "misc_net", "US",
         latitude, longitude, 1),
        # Past the 120 day claim window.
        (stale, 610.25, "Terry-Huel Store", "shopping_net", "US", latitude, longitude, 1),
    ]

    return [
        (
            f"txn_{first_sequence + index:06d}",
            account["account_id"],
            card_id,
            timestamp.strftime(TIMESTAMP),
            amount,
            merchant,
            category,
            channel_of(category),
            country,
            merchant_lat,
            merchant_lon,
            is_fraud,
        )
        for index, (
            timestamp, amount, merchant, category, country, merchant_lat, merchant_lon, is_fraud
        ) in enumerate(rows, start=1)
    ]


def build_database(
    db_path: Path | str | None = None, now: datetime | None = None
) -> SeedSummary:
    """Rebuilds the database from the seed extract, replacing any existing file."""
    destination = Path(db_path) if db_path is not None else database_path()
    moment = (now or datetime.now()).replace(microsecond=0)

    accounts = _read("accounts")
    cards = _read("cards")
    transactions = _read("transactions")

    # A single delta for everything, so the relative distances between transactions (which is
    # what the rules and the model actually look at) stay exactly as the dataset had them.
    newest = max(datetime.strptime(row["ts"], TIMESTAMP) for row in transactions)
    shift = moment - newest

    account_rows = [
        (
            row["account_id"],
            row["holder_name"],
            row["city"],
            row["state"],
            float(row["lat"]),
            float(row["lon"]),
            int(row["city_pop"]),
            (datetime.strptime(row["dob"], DATE) + shift).strftime(DATE),
            row["gender"],
            row["job"],
            (datetime.strptime(row["opened_at"], TIMESTAMP) + shift).strftime(TIMESTAMP),
        )
        for row in accounts
    ]
    card_rows = [
        (row["card_id"], row["account_id"], row["last4"], "active", None, None) for row in cards
    ]
    transaction_rows = [
        (
            row["txn_id"],
            row["account_id"],
            row["card_id"],
            (datetime.strptime(row["ts"], TIMESTAMP) + shift).strftime(TIMESTAMP),
            float(row["amount"]),
            row["merchant"],
            row["category"],
            row["channel"],
            row["country"],
            float(row["merch_lat"]),
            float(row["merch_lon"]),
            int(row["is_fraud"]),
        )
        for row in transactions
    ]

    demo_account = next(row for row in accounts if row["account_id"] == DEMO_ACCOUNT_ID)
    demo = _demo_rows(
        demo_account, f"card_{DEMO_ACCOUNT_ID}", len(transaction_rows), moment
    )
    transaction_rows.extend(demo)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    connection = sqlite3.connect(destination)
    connection.row_factory = sqlite3.Row
    try:
        create_schema(connection)
        connection.executemany(
            "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", account_rows
        )
        connection.executemany("INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?)", card_rows)
        connection.executemany(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            transaction_rows,
        )
        case_id, disputed = _preseed_state(connection, moment)
        connection.commit()

        span = connection.execute(
            "SELECT MIN(ts) AS first, MAX(ts) AS last FROM transactions"
        ).fetchone()
        first_ts, last_ts = span["first"], span["last"]
    finally:
        connection.close()

    return SeedSummary(
        database=destination,
        accounts=len(account_rows),
        transactions=len(transaction_rows),
        first_ts=first_ts,
        last_ts=last_ts,
        shift_days=shift.days,
        demo_account=DEMO_ACCOUNT_ID,
        demo_transactions=[
            (row[0], row[3], row[4], row[5], row[6]) for row in demo
        ],
        frozen_account=FROZEN_ACCOUNT_ID,
        case_id=case_id,
        case_account=CASE_ACCOUNT_ID,
        disputed_transaction=disputed,
    )


def _preseed_state(connection: sqlite3.Connection, now: datetime) -> tuple[str, str]:
    """Freezes one card and opens one case, so the tools have state to work with."""
    frozen_at = (now - timedelta(days=2)).replace(hour=9, minute=40, second=0)
    connection.execute(
        "UPDATE cards SET status = 'frozen', frozen_at = ?, freeze_reason = ? "
        "WHERE account_id = ?",
        (
            frozen_at.strftime(TIMESTAMP),
            "Preventive block after a confirmed card-not-present fraud report",
            FROZEN_ACCOUNT_ID,
        ),
    )

    # The dispute goes on the largest recent charge of the case account, which is the one an
    # analyst would have claimed first.
    disputed = connection.execute(
        "SELECT txn_id, amount FROM transactions WHERE account_id = ? AND ts >= ? "
        "ORDER BY amount DESC LIMIT 1",
        (CASE_ACCOUNT_ID, (now - timedelta(days=30)).strftime(TIMESTAMP)),
    ).fetchone()

    case_id = "CASE-0001"
    opened_at = (now - timedelta(days=1)).replace(hour=11, minute=15, second=0)
    reason = "Cardholder reported charges they do not recognise"
    connection.execute(
        "INSERT INTO cases (case_id, account_id, status, opened_at, reason) "
        "VALUES (?, ?, 'open', ?, ?)",
        (case_id, CASE_ACCOUNT_ID, opened_at.strftime(TIMESTAMP), reason),
    )
    connection.execute(
        "INSERT INTO disputes (dispute_id, case_id, txn_id, amount, reason, status, opened_at) "
        "VALUES ('DSP-0001', ?, ?, ?, ?, 'open', ?)",
        (case_id, disputed["txn_id"], disputed["amount"], reason, opened_at.strftime(TIMESTAMP)),
    )
    return case_id, disputed["txn_id"]


def ensure_database(db_path: Path | str | None = None) -> SeedSummary | None:
    """Builds the database if it is missing. Returns the summary only when it built one."""
    destination = Path(db_path) if db_path is not None else database_path()
    if destination.exists():
        return None
    return build_database(destination)


__all__ = [
    "ACCOUNT_COLUMNS",
    "CARD_COLUMNS",
    "DEFAULT_DB_PATH",
    "SEED_DIR",
    "TIMESTAMP",
    "TRANSACTION_COLUMNS",
    "SeedSummary",
    "build_database",
    "channel_of",
    "ensure_database",
]
