"""Extrae de los CSV crudos el subconjunto utilizado para la demostración.

Este extracto contiene veinte cuentas con su historial de movimientos.

Este script se corre una sola vez, en el entorno de desarrollo. Quien reconstruye la base
a partir del extracto es `fraud_mcp.infra.seed`.

Las marcas de tiempo se mantienen tal como vienen del dataset. Envejecerlas hasta el
presente es trabajo del sembrado, que ocurre más tarde y con otra fecha.

Uso:
    uv run python scripts/extract_seed.py
"""

from __future__ import annotations

import csv
import gzip
from pathlib import Path

import pandas as pd

from fraud_mcp.infra.seed import (
    ACCOUNT_COLUMNS,
    CARD_COLUMNS,
    SEED_DIR,
    TIMESTAMP,
    TRANSACTION_COLUMNS,
    channel_of,
)

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

# Se sirven veinte cuentas (suficiente para investigar y trivial de inspeccionar a mano).
CARD_COUNT = 20
FIRST_ACCOUNT_NUMBER = 4471

RAW_COLUMNS = [
    "trans_date_trans_time",
    "cc_num",
    "merchant",
    "category",
    "amt",
    "first",
    "last",
    "gender",
    "city",
    "state",
    "lat",
    "long",
    "city_pop",
    "job",
    "dob",
    "merch_lat",
    "merch_long",
    "is_fraud",
]


def load_raw() -> pd.DataFrame:
    frames = []
    for name in ("fraudTrain.csv", "fraudTest.csv"):
        frame = pd.read_csv(
            RAW / name,
            usecols=lambda column: column in RAW_COLUMNS,
            parse_dates=["trans_date_trans_time", "dob"],
        )
        print(f"  {name}: {len(frame):,} filas")
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def select_cards(data: pd.DataFrame) -> list[int]:
    """Elige las tarjetas con más fraude en su historial, de forma determinista.

    Se busca que las cuentas servidas tengan material real que investigar.
    """
    ranking = (
        data.groupby("cc_num")
        .agg(frauds=("is_fraud", "sum"), transactions=("is_fraud", "size"))
        .sort_values(["frauds", "transactions"], ascending=False)
    )
    return list(ranking.head(CARD_COUNT).index)


def build_rows(data: pd.DataFrame, cards: list[int]) -> tuple[list, list, list]:
    """Arma las filas de cuentas, tarjetas y movimientos, con las fechas del dataset."""
    subset = data[data["cc_num"].isin(cards)].copy()
    subset = subset.sort_values(["cc_num", "trans_date_trans_time"])

    accounts, card_rows, transactions = [], [], []
    sequence = 0

    for position, card_number in enumerate(cards):
        account_id = str(FIRST_ACCOUNT_NUMBER + position)
        card_id = f"card_{account_id}"
        history = subset[subset["cc_num"] == card_number]
        latest = history.iloc[-1]

        accounts.append({
            "account_id": account_id,
            "holder_name": f"{latest['first']} {latest['last']}",
            "city": latest["city"],
            "state": latest["state"],
            "lat": float(latest["lat"]),
            "lon": float(latest["long"]),
            "city_pop": int(latest["city_pop"]),
            "dob": latest["dob"].strftime("%Y-%m-%d"),
            "gender": latest["gender"],
            "job": latest["job"],
            "opened_at": history.iloc[0]["trans_date_trans_time"].strftime(TIMESTAMP),
        })
        card_rows.append({
            "card_id": card_id,
            "account_id": account_id,
            "last4": str(int(card_number))[-4:],
        })

        for row in history.itertuples(index=False):
            sequence += 1
            transactions.append({
                "txn_id": f"txn_{sequence:06d}",
                "account_id": account_id,
                "card_id": card_id,
                "ts": row.trans_date_trans_time.strftime(TIMESTAMP),
                "amount": round(float(row.amt), 2),
                "merchant": row.merchant.replace("fraud_", ""),
                "category": row.category,
                "channel": channel_of(row.category),
                "country": "US",
                "merch_lat": float(row.merch_lat),
                "merch_lon": float(row.merch_long),
                "is_fraud": int(row.is_fraud),
            })

    return accounts, card_rows, transactions


def write(name: str, columns: list[str], rows: list[dict]) -> Path:
    destination = SEED_DIR / f"{name}.csv.gz"
    with gzip.open(destination, "wt", encoding="utf-8", newline="", compresslevel=9) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return destination


def main() -> None:
    print("Cargando archivos crudos")
    data = load_raw()

    cards = select_cards(data)
    print(f"Seleccionadas {len(cards)} tarjetas")

    accounts, card_rows, transactions = build_rows(data, cards)

    SEED_DIR.mkdir(parents=True, exist_ok=True)
    written = [
        write("accounts", ACCOUNT_COLUMNS, accounts),
        write("cards", CARD_COLUMNS, card_rows),
        write("transactions", TRANSACTION_COLUMNS, transactions),
    ]

    print(f"\nEscrito {SEED_DIR.relative_to(ROOT)}")
    for path in written:
        print(f"  {path.name:<22} {path.stat().st_size / 1024:>8,.0f} KB")
    print(
        f"\n{len(accounts)} cuentas ({accounts[0]['account_id']} a "
        f"{accounts[-1]['account_id']}), {len(transactions):,} movimientos, "
        f"del {transactions[0]['ts']} al {max(row['ts'] for row in transactions)}"
    )
    print("Construir la base con: uv run python scripts/seed_db.py --force")


if __name__ == "__main__":
    main()
