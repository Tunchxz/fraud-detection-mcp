"""Combines the model score with the explicit rules into one verdict.

This is the hybrid approach. The model orders transactions by probability, and the rules
supply auditable reasons. Neither replaces the other (a score of 0.97 with no rule fired
is still worth a look, and an impossible geovelocity is worth acting on independently of
what the model says).
"""

from __future__ import annotations

from dataclasses import dataclass

from fraud_mcp.domain import features, rules
from fraud_mcp.domain.model import RiskModel
from fraud_mcp.domain.models import Account, CardHistory, Transaction

HIGH = "high"
MEDIUM = "medium"
LOW = "low"


@dataclass(frozen=True)
class Verdict:
    score: float
    threshold: float
    level: str
    exceeds_threshold: bool
    hits: list[rules.RuleHit]
    drivers: list[tuple[str, float]]

    def to_dict(self) -> dict:
        return {
            "risk_score": round(self.score, 4),
            "threshold": self.threshold,
            "risk_level": self.level,
            "exceeds_threshold": self.exceeds_threshold,
            "triggered_rules": [hit.to_dict() for hit in self.hits],
            "model_drivers": [
                {"feature": name, "contribution": value} for name, value in self.drivers
            ],
        }


def assess(
    transaction: Transaction, account: Account, history: CardHistory, model: RiskModel
) -> Verdict:
    """Scores a transaction and classifies it.

    The level is deterministic and does not depend on the model alone:

    - `high`: the score reaches the model's threshold, or a high severity rule fired;
    - `medium`: the score is at least half the threshold, or two or more rules fired;
    - `low`: anything else.
    """
    vector = features.build(transaction, account, history, model)
    score = model.score(vector)
    hits = rules.evaluate(transaction, account, history)

    exceeds = score >= model.threshold
    has_severe_rule = any(hit.severity == HIGH for hit in hits)

    if exceeds or has_severe_rule:
        level = HIGH
    elif score >= model.threshold / 2 or len(hits) >= 2:
        level = MEDIUM
    else:
        level = LOW

    return Verdict(
        score=score,
        threshold=model.threshold,
        level=level,
        exceeds_threshold=exceeds,
        hits=hits,
        drivers=model.drivers(vector),
    )
