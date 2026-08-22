"""Construye `data/fraud.db` a partir del extracto versionado e imprime un reporte.

El sembrado en sí vive en `fraud_mcp.infra.seed`, porque el servidor remoto también lo
necesita al arrancar. Este script existe para las dos cosas que el sembrado automático NO
debe hacer:

- reconstruir a la fuerza (`--force`), que es la forma de resetear el estado después de
  una demostración y de refrescar las fechas;
- emitir el informe de cobertura, que recorre los movimientos recientes con las reglas del
  servidor y dice qué cuenta demuestra qué. Verifica la cobertura y sirve para saber dónde
  probar cada cosa.

Uso:
    uv run python scripts/seed_db.py            # siembra si la base no existe
    uv run python scripts/seed_db.py --force    # reconstruye siempre
    uv run python scripts/seed_db.py --quiet    # sin informe de cobertura
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

from fraud_mcp.domain import rules
from fraud_mcp.domain.models import Account, CardHistory, Transaction
from fraud_mcp.infra import db
from fraud_mcp.infra.seed import SeedSummary, build_database, ensure_database

ROOT = Path(__file__).resolve().parents[1]

# Ventana sobre la que se mide la cobertura de reglas.
REPORT_DAYS = 30

ALL_RULES = (
    "impossible_geovelocity",
    "amount_vs_profile",
    "rapid_succession",
    "night_window",
    "high_risk_category",
    "high_amount",
)


def coverage(connection: sqlite3.Connection, days: int) -> dict[str, dict[str, int]]:
    """Cuenta, por cuenta, cuántos movimientos recientes dispara cada regla."""
    accounts = {
        row["account_id"]: Account.from_row(row)
        for row in connection.execute("SELECT * FROM accounts")
    }
    recent = connection.execute(
        "SELECT * FROM transactions WHERE ts >= datetime('now', ?) ORDER BY card_id, ts",
        (f"-{days} days",),
    ).fetchall()

    fired: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in recent:
        transaction = Transaction.from_row(row)
        previous = db.previous_transaction(connection, transaction.card_id, row["ts"])
        prior_count, prior_mean = db.prior_amount_stats(
            connection, transaction.card_id, row["ts"]
        )
        history = CardHistory(
            prior_count=prior_count,
            prior_mean=prior_mean,
            previous=Transaction.from_row(previous) if previous is not None else None,
        )
        for hit in rules.evaluate(transaction, accounts[transaction.account_id], history):
            fired[transaction.account_id][hit.rule] += 1

    return fired


def print_summary(summary: SeedSummary) -> None:
    print(f"\nEscrito {summary.database}")
    print(f"  cuentas:     {summary.accounts}")
    print(f"  movimientos: {summary.transactions:,}")
    print(f"  período:     {summary.first_ts} → {summary.last_ts}")
    print(f"  desplazamiento aplicado al dataset: {summary.shift_days:,} días")

    print(f"\nCaso inyectado en la cuenta {summary.demo_account}")
    for txn_id, timestamp, amount, merchant, category in summary.demo_transactions:
        print(f"  {txn_id}  {timestamp}  {amount:>8,.2f} USD  {merchant} ({category})")

    print("\nEstado pre-sembrado")
    print(f"  cuenta {summary.frozen_account}: tarjeta bloqueada")
    print(
        f"  cuenta {summary.case_account}: {summary.case_id} abierto sobre "
        f"{summary.disputed_transaction}"
    )


def print_coverage(connection: sqlite3.Connection, days: int) -> None:
    fired = coverage(connection, days)

    print(f"\nCobertura de reglas en los últimos {days} días")
    for rule in ALL_RULES:
        cuentas = sorted(account for account in fired if fired[account].get(rule))
        movimientos = sum(fired[account].get(rule, 0) for account in fired)
        marca = "ok" if cuentas else "SIN COBERTURA"
        print(
            f"  {rule:<24} {len(cuentas):>2} cuentas, {movimientos:>4} movimientos  "
            f"[{marca}] {', '.join(cuentas[:5])}"
        )

    completas = sorted(a for a in fired if len(fired[a]) == len(ALL_RULES))
    vacias = sorted(
        row["account_id"]
        for row in connection.execute("SELECT account_id FROM accounts")
        if row["account_id"] not in fired
    )
    print(f"\n  cuentas que disparan las seis reglas: {len(completas)} ({', '.join(completas[:8])})")
    if vacias:
        print(f"  cuentas sin alertas recientes: {', '.join(vacias)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force", action="store_true", help="reconstruye la base aunque ya exista"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="omite el informe de cobertura"
    )
    arguments = parser.parse_args()

    if arguments.force:
        summary = build_database()
    else:
        summary = ensure_database()
        if summary is None:
            print(
                f"{db.database_path()} ya existe; no se reconstruye. "
                "Usar --force para rehacerla."
            )
            return

    print_summary(summary)

    if not arguments.quiet:
        connection = db.connect(summary.database)
        try:
            print_coverage(connection, REPORT_DAYS)
        finally:
            connection.close()


if __name__ == "__main__":
    main()
