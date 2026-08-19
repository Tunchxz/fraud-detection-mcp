"""Domain entities.

Plain dataclasses built from database rows. The domain never imports the protocol layer
nor the database module, it only receives mappings and returns values (which is what
makes the rules testable without a server or a connection).

`Transaction` deliberately has no `is_fraud` field. The label lives in the database for
seeding purposes only; a verdict must come from the model and the rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

TIMESTAMP = "%Y-%m-%d %H:%M:%S"


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP)


@dataclass(frozen=True)
class Account:
    account_id: str
    holder_name: str
    city: str
    state: str
    lat: float
    lon: float
    city_pop: int
    dob: date
    gender: str
    job: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Account:
        return cls(
            account_id=row["account_id"],
            holder_name=row["holder_name"],
            city=row["city"],
            state=row["state"],
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            city_pop=int(row["city_pop"]),
            dob=datetime.strptime(row["dob"], "%Y-%m-%d").date(),
            gender=row["gender"],
            job=row["job"],
        )


@dataclass(frozen=True)
class Card:
    card_id: str
    account_id: str
    last4: str
    status: str
    frozen_at: str | None
    freeze_reason: str | None

    @property
    def is_frozen(self) -> bool:
        return self.status == "frozen"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Card:
        return cls(
            card_id=row["card_id"],
            account_id=row["account_id"],
            last4=row["last4"],
            status=row["status"],
            frozen_at=row["frozen_at"],
            freeze_reason=row["freeze_reason"],
        )


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    account_id: str
    card_id: str
    ts: datetime
    amount: float
    merchant: str
    category: str
    channel: str
    country: str
    merch_lat: float
    merch_lon: float

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Transaction:
        return cls(
            txn_id=row["txn_id"],
            account_id=row["account_id"],
            card_id=row["card_id"],
            ts=parse_timestamp(row["ts"]),
            amount=float(row["amount"]),
            merchant=row["merchant"],
            category=row["category"],
            channel=row["channel"],
            country=row["country"],
            merch_lat=float(row["merch_lat"]),
            merch_lon=float(row["merch_lon"]),
        )

    def summary(self) -> dict:
        """The shape the tools expose. Never includes the fraud label."""
        return {
            "transaction_id": self.txn_id,
            "timestamp": self.ts.strftime(TIMESTAMP),
            "amount": round(self.amount, 2),
            "merchant": self.merchant,
            "category": self.category,
            "channel": self.channel,
            "country": self.country,
        }


@dataclass(frozen=True)
class CardHistory:
    """What the card did before the transaction under analysis.

    `prior_count` and `prior_mean` mirror the shifted expanding mean used at training time:
    they cover earlier transactions only.
    """

    prior_count: int
    prior_mean: float | None
    previous: Transaction | None
