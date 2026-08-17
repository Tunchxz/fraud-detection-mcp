"""Construye `data/fraud.db`, la base sobre la que opera el servidor MCP.

Toma un subconjunto pequeño del dataset, lo reorganiza en el modelo de datos de un centro
de monitoreo (cuentas, tarjetas, movimientos, casos, disputas) e inyecta un caso de prueba
para demostración.

Notas:

- Las fechas se desplazan al presente. El dataset termina el 2020-12-31, así que todas
  las marcas de tiempo se corren para que la última caiga en el momento del sembrado.
  La fecha de nacimiento se desplaza con el mismo delta para que la edad del titular siga
  siendo la que vio el modelo al entrenarse.
- Todas las marcas de tiempo son hora local sin zona, tanto localmente como en el servidor.

Uso:
    uv run python scripts/seed_db.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from fraud_mcp.domain.rules import haversine_km
from fraud_mcp.infra.db import DEFAULT_DB_PATH, create_schema

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

# Se sirven veinte cuentas
CARD_COUNT = 20
FIRST_ACCOUNT_NUMBER = 4471  # mejora para el escenario de demostración
DEMO_ACCOUNT_ID = str(FIRST_ACCOUNT_NUMBER)

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

TIMESTAMP = "%Y-%m-%d %H:%M:%S"

# Coordenadas usadas por el caso inyectado.
GUATEMALA_CITY = (14.6349, -90.5069)


def channel_of(category: str) -> str:
    """Las categorías `_net` son compras en línea; el resto son presenciales.

    Es una simplificación deliberada y documentada: el dataset no trae el canal, y la
    distinción importa porque la regla de geovelocidad solo tiene sentido entre compras
    presenciales.
    """
    return "online" if category.endswith("_net") else "in_person"


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

    Se busca que las cuentas servidas tengan material real que investigar; el desempate por
    número de tarjeta hace que el resultado no dependa del orden de lectura.
    """
    ranking = (
        data.groupby("cc_num")
        .agg(frauds=("is_fraud", "sum"), transactions=("is_fraud", "size"))
        .sort_values(["frauds", "transactions"], ascending=False)
    )
    return list(ranking.head(CARD_COUNT).index)


def build_rows(data: pd.DataFrame, cards: list[int]) -> tuple[list, list, list]:
    """Arma las filas de cuentas, tarjetas y movimientos, ya con las fechas desplazadas."""
    subset = data[data["cc_num"].isin(cards)].copy()
    shift = datetime.now().replace(microsecond=0) - subset["trans_date_trans_time"].max()
    subset["ts"] = subset["trans_date_trans_time"] + shift
    subset = subset.sort_values(["cc_num", "ts"])

    accounts, card_rows, transactions = [], [], []
    sequence = 0

    for position, card_number in enumerate(cards):
        account_id = str(FIRST_ACCOUNT_NUMBER + position)
        card_id = f"card_{account_id}"
        history = subset[subset["cc_num"] == card_number]
        latest = history.iloc[-1]

        accounts.append((
            account_id,
            f"{latest['first']} {latest['last']}",
            latest["city"],
            latest["state"],
            float(latest["lat"]),
            float(latest["long"]),
            int(latest["city_pop"]),
            (latest["dob"] + shift).strftime("%Y-%m-%d"),
            latest["gender"],
            latest["job"],
            history.iloc[0]["ts"].strftime(TIMESTAMP),
        ))
        card_rows.append((card_id, account_id, str(int(card_number))[-4:], "active", None, None))

        for row in history.itertuples(index=False):
            sequence += 1
            transactions.append((
                f"txn_{sequence:06d}",
                account_id,
                card_id,
                row.ts.strftime(TIMESTAMP),
                round(float(row.amt), 2),
                row.merchant.replace("fraud_", ""),
                row.category,
                channel_of(row.category),
                "US",
                float(row.merch_lat),
                float(row.merch_long),
                int(row.is_fraud),
            ))

    return accounts, card_rows, transactions


def demo_transactions(account: tuple, first_sequence: int) -> list[tuple]:
    """El caso de la propuesta, inyectado en la cuenta de la demostración.

    Contiene tres piezas: un par de compras presenciales con geovelocidad imposible, dos
    compras en línea de monto alto en horario nocturno para que el modelo tenga qué puntuar,
    y una transacción antigua que servirá para comprobar el rechazo por plazo de reclamo.
    """
    account_id, _holder, city, _state, lat, lon = account[:6]
    card_id = f"card_{account_id}"
    now = datetime.now().replace(microsecond=0)

    home_leg = (now - timedelta(days=6)).replace(hour=3, minute=12, second=0)
    abroad_leg = home_leg + timedelta(minutes=14)
    night = (now - timedelta(days=5)).replace(hour=23, minute=31, second=0)
    stale = (now - timedelta(days=200)).replace(hour=22, minute=10, second=0)

    rows = [
        # Compra presencial normal, en la ciudad del titular.
        (home_leg, 62.40, f"Kozey-Boyle Market {city}", "grocery_pos", "US", lat, lon, 0),
        # Catorce minutos después, otra compra presencial a miles de kilómetros.
        (abroad_leg, 318.75, "Estacion Zona 10", "gas_transport", "GT", *GUATEMALA_CITY, 1),
        (night, 1180.00, "Cormier-Dickerson Online", "shopping_net", "US", lat, lon, 1),
        (night + timedelta(minutes=16), 940.50, "Rutherford Digital", "misc_net", "US", lat, lon, 1),
        # Fuera del plazo de reclamo de 120 días.
        (stale, 610.25, "Terry-Huel Store", "shopping_net", "US", lat, lon, 1),
    ]

    return [
        (
            f"txn_{first_sequence + index:06d}",
            account_id,
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
            timestamp,
            amount,
            merchant,
            category,
            country,
            merchant_lat,
            merchant_lon,
            is_fraud,
        ) in enumerate(rows, start=1)
    ]


def main() -> None:
    print("Cargando archivos crudos")
    data = load_raw()

    cards = select_cards(data)
    print(f"Seleccionadas {len(cards)} tarjetas")

    accounts, card_rows, transactions = build_rows(data, cards)
    demo = demo_transactions(accounts[0], len(transactions))
    transactions.extend(demo)

    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_DB_PATH.unlink(missing_ok=True)

    connection = sqlite3.connect(DEFAULT_DB_PATH)
    connection.row_factory = sqlite3.Row
    create_schema(connection)
    connection.executemany(
        "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", accounts
    )
    connection.executemany("INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?)", card_rows)
    connection.executemany(
        "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", transactions
    )
    connection.commit()

    span = connection.execute(
        "SELECT MIN(ts) AS first, MAX(ts) AS last FROM transactions"
    ).fetchone()
    connection.close()

    home_leg, abroad_leg = demo[0], demo[1]
    distance = haversine_km(home_leg[9], home_leg[10], abroad_leg[9], abroad_leg[10])
    minutes = (
        datetime.strptime(abroad_leg[3], TIMESTAMP) - datetime.strptime(home_leg[3], TIMESTAMP)
    ).total_seconds() / 60

    print(f"\nEscrito {DEFAULT_DB_PATH.relative_to(ROOT)}")
    print(f"  cuentas:     {len(accounts)} ({accounts[0][0]} a {accounts[-1][0]})")
    print(f"  movimientos: {len(transactions):,}")
    print(f"  período:     {span['first']} → {span['last']}")
    print(f"\nCaso inyectado en la cuenta {DEMO_ACCOUNT_ID} ({accounts[0][1]}, {accounts[0][2]})")
    print(f"  {home_leg[0]}  {home_leg[3]}  {home_leg[4]:>8.2f} USD  {home_leg[5]} ({home_leg[8]})")
    print(f"  {abroad_leg[0]}  {abroad_leg[3]}  {abroad_leg[4]:>8.2f} USD  {abroad_leg[5]} ({abroad_leg[8]})")
    print(f"  → {distance:,.0f} km en {minutes:.0f} minutos = {distance / (minutes / 60):,.0f} km/h")
    for row in demo[2:]:
        print(f"  {row[0]}  {row[3]}  {row[4]:>8.2f} USD  {row[5]} ({row[6]})")


if __name__ == "__main__":
    main()
