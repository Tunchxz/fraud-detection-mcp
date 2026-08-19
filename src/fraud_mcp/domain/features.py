"""Builds the model input vector.

This is the runtime half of the contract declared in `scripts/prepare_dataset.py`. The same
24 variables, computed the same way, in the order the artifact declares. If the two ever
drift apart, the score the analyst sees stops matching the model that was validated.
"""

from __future__ import annotations

import math

from fraud_mcp.domain.model import RiskModel
from fraud_mcp.domain.models import Account, CardHistory, Transaction


def build(
    transaction: Transaction, account: Account, history: CardHistory, model: RiskModel
) -> dict[str, float]:
    """Returns the feature vector for one transaction."""
    neutral_ratio = model.neutral_values["amt_ratio_prev_mean"]
    neutral_minutes = model.neutral_values["minutes_since_prev"]

    if history.prior_count > 0 and history.prior_mean:
        amount_ratio = transaction.amount / history.prior_mean
    else:
        amount_ratio = neutral_ratio  # first transaction of the card (no history to compare)

    if history.previous is not None:
        minutes_since_previous = (
            transaction.ts - history.previous.ts
        ).total_seconds() / 60
    else:
        minutes_since_previous = neutral_minutes

    timestamp = transaction.ts
    vector: dict[str, float] = {
        "log_amt": math.log1p(transaction.amount),
        "hour": timestamp.hour,
        "is_night": 1.0 if timestamp.hour in model.night_hours else 0.0,
        "day_of_week": timestamp.weekday(),
        "is_weekend": 1.0 if timestamp.weekday() >= 5 else 0.0,
        "age": (timestamp.date() - account.dob).days / 365.25,
        "log_city_pop": math.log1p(account.city_pop),
        "gender_m": 1.0 if account.gender == "M" else 0.0,
        "amt_ratio_prev_mean": amount_ratio,
        "minutes_since_prev": minutes_since_previous,
    }
    for category in model.categories:
        vector[f"cat_{category}"] = 1.0 if transaction.category == category else 0.0

    missing = set(model.features) - set(vector)
    extra = set(vector) - set(model.features)
    if missing or extra:
        raise ValueError(
            "Feature vector does not match the model contract. "
            f"Missing: {sorted(missing)}. Unexpected: {sorted(extra)}"
        )

    return vector
