"""Explicit fraud rules.

A probability alone cannot be explained to a customer or an auditor, so the score is
accompanied by rules that state, in words, what is wrong with a transaction. Every
threshold here comes from the exploratory analysis in `notebooks/01_eda.ipynb`.

This module only takes entities and returns findings, with no database, no protocol and no
model involved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from fraud_mcp.domain.models import Account, CardHistory, Transaction

# Velocidad de crucero de un avión comercial. En el EDA (#9), el 8.54% de los fraudes exige
# superarla frente al 2.11% de las transacciones legítimas.
IMPOSSIBLE_SPEED_KMH = 900.0

# El EDA (#9) midió una razón mediana de 5.23 veces el gasto habitual en los fraudes, frente
# a 0.66 en las transacciones legítimas.
AMOUNT_RATIO_ALERT = 5.0

# Franja donde se concentra el fraude: 22 y 23 horas al 2.88% y 2.84%, y la madrugada de 0 a
# 3 alrededor del 1.5%, contra un promedio general de 0.58% (EDA #6).
NIGHT_HOURS = frozenset({22, 23, 0, 1, 2, 3})

# Las tres categorías con mayor tasa de fraude del EDA (#7).
HIGH_RISK_CATEGORIES = ("shopping_net", "misc_net", "grocery_pos")

# Los fraudes llegan más pegados al movimiento anterior. Mediana de 82 minutos contra
# 277 (EDA #9).
RAPID_SUCCESSION_MINUTES = 10.0

# A partir de 200 USD la tasa de fraude salta a 4.44%, y sobre 500 USD supera el 23% (EDA #5).
HIGH_AMOUNT_USD = 200.0


@dataclass(frozen=True)
class RuleHit:
    """A rule that fired, with the evidence that explains it."""

    rule: str
    reason: str
    severity: str  # "high" | "medium"
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance over the surface of the Earth, in kilometres."""
    radius = 6371.0
    phi1, lambda1, phi2, lambda2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin((lambda2 - lambda1) / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def implied_speed_kmh(current: Transaction, previous: Transaction) -> float | None:
    """Speed the cardholder would have had to travel between two card-present purchases.

    Returns None when the pair cannot be judged (either purchase made online, or no time
    between them).
    """
    if current.channel != "in_person" or previous.channel != "in_person":
        return None

    hours = (current.ts - previous.ts).total_seconds() / 3600
    if hours <= 0:
        return None

    distance = haversine_km(
        previous.merch_lat, previous.merch_lon, current.merch_lat, current.merch_lon
    )
    return distance / hours


def evaluate(
    transaction: Transaction, account: Account, history: CardHistory
) -> list[RuleHit]:
    """Runs every rule and returns the ones that fired, most severe first."""
    hits: list[RuleHit] = []
    previous = history.previous

    if previous is not None:
        speed = implied_speed_kmh(transaction, previous)
        if speed is not None and speed > IMPOSSIBLE_SPEED_KMH:
            distance = haversine_km(
                previous.merch_lat, previous.merch_lon, transaction.merch_lat, transaction.merch_lon
            )
            minutes = (transaction.ts - previous.ts).total_seconds() / 60
            hits.append(RuleHit(
                rule="impossible_geovelocity",
                severity="high",
                reason=(
                    f"Card-present purchase {distance:,.0f} km away from the previous one "
                    f"({previous.country} → {transaction.country}) only {minutes:.0f} minutes "
                    f"later, which implies travelling at {speed:,.0f} km/h."
                ),
                evidence={
                    "previous_transaction_id": previous.txn_id,
                    "distance_km": round(distance, 1),
                    "minutes_apart": round(minutes, 1),
                    "implied_speed_kmh": round(speed, 1),
                },
            ))

        minutes_apart = (transaction.ts - previous.ts).total_seconds() / 60
        if 0 <= minutes_apart < RAPID_SUCCESSION_MINUTES:
            hits.append(RuleHit(
                rule="rapid_succession",
                severity="medium",
                reason=(
                    f"Only {minutes_apart:.0f} minutes since the previous transaction on the "
                    f"same card, below the {RAPID_SUCCESSION_MINUTES:.0f} minute threshold."
                ),
                evidence={
                    "previous_transaction_id": previous.txn_id,
                    "minutes_apart": round(minutes_apart, 1),
                },
            ))

    if history.prior_count > 0 and history.prior_mean:
        ratio = transaction.amount / history.prior_mean
        if ratio >= AMOUNT_RATIO_ALERT:
            hits.append(RuleHit(
                rule="amount_vs_profile",
                severity="high",
                reason=(
                    f"Amount of {transaction.amount:,.2f} USD is {ratio:.1f} times the "
                    f"card's historical average of {history.prior_mean:,.2f} USD."
                ),
                evidence={
                    "amount": round(transaction.amount, 2),
                    "historical_average": round(history.prior_mean, 2),
                    "ratio": round(ratio, 2),
                },
            ))

    if transaction.ts.hour in NIGHT_HOURS:
        hits.append(RuleHit(
            rule="night_window",
            severity="medium",
            reason=(
                f"Transaction at {transaction.ts.strftime('%H:%M')}, inside the 22:00-03:59 "
                "window where the fraud rate is about five times the average."
            ),
            evidence={"hour": transaction.ts.hour},
        ))

    if transaction.category in HIGH_RISK_CATEGORIES:
        hits.append(RuleHit(
            rule="high_risk_category",
            severity="medium",
            reason=(
                f"Merchant category '{transaction.category}' is among the three with the "
                "highest historical fraud rate."
            ),
            evidence={"category": transaction.category},
        ))

    if transaction.amount >= HIGH_AMOUNT_USD:
        hits.append(RuleHit(
            rule="high_amount",
            severity="medium",
            reason=(
                f"Amount of {transaction.amount:,.2f} USD is above the {HIGH_AMOUNT_USD:,.0f} "
                "USD mark, where the historical fraud rate multiplies by eight."
            ),
            evidence={"amount": round(transaction.amount, 2)},
        ))

    hits.sort(key=lambda hit: 0 if hit.severity == "high" else 1)
    return hits
