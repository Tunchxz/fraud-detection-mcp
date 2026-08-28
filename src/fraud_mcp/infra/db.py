"""SQLite access: schema, connection and queries.

The host launches the server with an arbitrary working directory, so the database path is
resolved relative to this package and can be overridden with `FRAUD_MCP_DB`.

The `transactions.is_fraud` column holds the ground truth of the dataset. It is kept because
the seeding script needs it to build the demonstration case, and it is never exposed
by a tool (the server reaches its verdict from the model and the rules, not from the
label).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "fraud.db"

SCHEMA = """
CREATE TABLE accounts (
    account_id  TEXT PRIMARY KEY,
    holder_name TEXT NOT NULL,
    city        TEXT NOT NULL,
    state       TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    city_pop    INTEGER NOT NULL,
    dob         TEXT NOT NULL,
    gender      TEXT NOT NULL CHECK (gender IN ('M', 'F')),
    job         TEXT NOT NULL,
    opened_at   TEXT NOT NULL
);

CREATE TABLE cards (
    card_id       TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts (account_id),
    last4         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active', 'frozen')),
    frozen_at     TEXT,
    freeze_reason TEXT
);

CREATE TABLE transactions (
    txn_id     TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    card_id    TEXT NOT NULL REFERENCES cards (card_id),
    ts         TEXT NOT NULL,
    amount     REAL NOT NULL,
    merchant   TEXT NOT NULL,
    category   TEXT NOT NULL,
    channel    TEXT NOT NULL CHECK (channel IN ('online', 'in_person')),
    country    TEXT NOT NULL,
    merch_lat  REAL NOT NULL,
    merch_lon  REAL NOT NULL,
    is_fraud   INTEGER NOT NULL
);

CREATE INDEX idx_transactions_account_ts ON transactions (account_id, ts);
CREATE INDEX idx_transactions_card_ts ON transactions (card_id, ts);

CREATE TABLE cases (
    case_id    TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts (account_id),
    status     TEXT NOT NULL CHECK (status IN ('open', 'under_review', 'closed')),
    opened_at  TEXT NOT NULL,
    reason     TEXT NOT NULL
);

CREATE TABLE disputes (
    dispute_id TEXT PRIMARY KEY,
    case_id    TEXT NOT NULL REFERENCES cases (case_id),
    txn_id     TEXT NOT NULL UNIQUE REFERENCES transactions (txn_id),
    amount     REAL NOT NULL,
    reason     TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'rejected')),
    opened_at  TEXT NOT NULL
);

CREATE TABLE freeze_tokens (
    token      TEXT PRIMARY KEY,
    card_id    TEXT NOT NULL REFERENCES cards (card_id),
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at    TEXT
);
"""


def database_path() -> Path:
    override = os.environ.get("FRAUD_MCP_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def connect(path: Path | str | None = None, *, same_thread: bool = True) -> sqlite3.Connection:
    """Opens the database. Rows come back as mappings and foreign keys are enforced.

    The connection is threaded, so it is reached from whichever thread is serving the request.
    The transport serialises every dispatch behind a lock, which is the guarantee `sqlite3` asks
    for in exchange.
    """
    location = Path(path) if path is not None else database_path()
    if not location.exists():
        raise FileNotFoundError(
            f"Database not found at {location}. Run 'python scripts/seed_db.py' to build "
            "it, or point FRAUD_MCP_DB at an existing file."
        )

    connection = sqlite3.connect(location, check_same_thread=same_thread)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


# --- Reads -------------------------------------------------------------------------


def fetch_account(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()


def fetch_card(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM cards WHERE account_id = ?", (account_id,)
    ).fetchone()


def fetch_transaction(connection: sqlite3.Connection, txn_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()


def fetch_transactions(
    connection: sqlite3.Connection,
    account_id: str,
    since: str,
    limit: int,
    min_amount: float | None = None,
    channel: str | None = None,
) -> list[sqlite3.Row]:
    """Most recent transactions of an account from `since` onwards."""
    query = "SELECT * FROM transactions WHERE account_id = ? AND ts >= ?"
    parameters: list[object] = [account_id, since]

    if min_amount is not None:
        query += " AND amount >= ?"
        parameters.append(min_amount)
    if channel is not None:
        query += " AND channel = ?"
        parameters.append(channel)

    query += " ORDER BY ts DESC LIMIT ?"
    parameters.append(limit)
    return connection.execute(query, parameters).fetchall()


def previous_transaction(
    connection: sqlite3.Connection, card_id: str, ts: str
) -> sqlite3.Row | None:
    """The transaction immediately before `ts` on the same card."""
    return connection.execute(
        "SELECT * FROM transactions WHERE card_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
        (card_id, ts),
    ).fetchone()


def prior_amount_stats(
    connection: sqlite3.Connection, card_id: str, ts: str
) -> tuple[int, float | None]:
    """Count and mean of the amounts *before* `ts` on the same card.

    This is the runtime equivalent of the shifted expanding mean used at training time in
    `scripts/prepare_dataset.py`: only earlier transactions take part.
    """
    row = connection.execute(
        "SELECT COUNT(*) AS n, AVG(amount) AS mean FROM transactions "
        "WHERE card_id = ? AND ts < ?",
        (card_id, ts),
    ).fetchone()
    return int(row["n"]), row["mean"]


def spending_profile(connection: sqlite3.Connection, account_id: str, since: str) -> sqlite3.Row:
    """Aggregate spending of an account from `since` onwards."""
    return connection.execute(
        "SELECT COUNT(*) AS transactions, AVG(amount) AS average_amount, "
        "MAX(amount) AS largest_amount, SUM(amount) AS total_amount "
        "FROM transactions WHERE account_id = ? AND ts >= ?",
        (account_id, since),
    ).fetchone()


def top_categories(
    connection: sqlite3.Connection, account_id: str, since: str, limit: int = 3
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT category, COUNT(*) AS transactions, SUM(amount) AS total_amount "
        "FROM transactions WHERE account_id = ? AND ts >= ? "
        "GROUP BY category ORDER BY total_amount DESC LIMIT ?",
        (account_id, since, limit),
    ).fetchall()


def fetch_case(connection: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()


def fetch_case_disputes(connection: sqlite3.Connection, case_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT d.*, t.merchant, t.ts AS transaction_ts FROM disputes d "
        "JOIN transactions t ON t.txn_id = d.txn_id "
        "WHERE d.case_id = ? ORDER BY d.opened_at",
        (case_id,),
    ).fetchall()


def open_case_for_account(
    connection: sqlite3.Connection, account_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM cases WHERE account_id = ? AND status != 'closed' "
        "ORDER BY opened_at DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def count_open_cases(connection: sqlite3.Connection, account_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM cases WHERE account_id = ? AND status != 'closed'",
        (account_id,),
    ).fetchone()
    return int(row["n"])


def dispute_for_transaction(
    connection: sqlite3.Connection, txn_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM disputes WHERE txn_id = ?", (txn_id,)
    ).fetchone()


def fetch_freeze_token(connection: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM freeze_tokens WHERE token = ?", (token,)
    ).fetchone()


# --- Writes ------------------------------------------------------------------------


def store_freeze_token(
    connection: sqlite3.Connection, token: str, card_id: str, reason: str, created_at: str
) -> None:
    connection.execute(
        "INSERT INTO freeze_tokens (token, card_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (token, card_id, reason, created_at),
    )
    connection.commit()


def consume_freeze_token(connection: sqlite3.Connection, token: str, used_at: str) -> None:
    connection.execute("UPDATE freeze_tokens SET used_at = ? WHERE token = ?", (used_at, token))
    connection.commit()


def freeze_card(
    connection: sqlite3.Connection, card_id: str, reason: str, frozen_at: str
) -> None:
    connection.execute(
        "UPDATE cards SET status = 'frozen', frozen_at = ?, freeze_reason = ? WHERE card_id = ?",
        (frozen_at, reason, card_id),
    )
    connection.commit()


def insert_case(
    connection: sqlite3.Connection, case_id: str, account_id: str, reason: str, opened_at: str
) -> None:
    connection.execute(
        "INSERT INTO cases (case_id, account_id, status, opened_at, reason) "
        "VALUES (?, ?, 'open', ?, ?)",
        (case_id, account_id, opened_at, reason),
    )
    connection.commit()


def insert_dispute(
    connection: sqlite3.Connection,
    dispute_id: str,
    case_id: str,
    txn_id: str,
    amount: float,
    reason: str,
    opened_at: str,
) -> None:
    connection.execute(
        "INSERT INTO disputes (dispute_id, case_id, txn_id, amount, reason, status, opened_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (dispute_id, case_id, txn_id, amount, reason, opened_at),
    )
    connection.commit()


def next_sequence(connection: sqlite3.Connection, table: str) -> int:
    """Number of rows plus one, for building readable identifiers like CASE-0007."""
    if table not in ("cases", "disputes"):
        raise ValueError(f"Unsupported table for sequencing: {table}")
    row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608
    return int(row["n"]) + 1
