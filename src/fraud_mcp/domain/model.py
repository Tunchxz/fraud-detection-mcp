"""The trained risk model, evaluated in pure Python.

`artifacts/model.json` carries the coefficients, the scaling and the threshold produced by
`scripts/train_model.py`. Evaluating a logistic regression is a standardisation, so the 
server needs no third-party library.

The arithmetic here matches `score_with_pure_python()` in `scripts/train_model.py`, which
is checked against scikit-learn on every training run.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_PATH = REPO_ROOT / "artifacts" / "model.json"


def model_path() -> Path:
    override = os.environ.get("FRAUD_MCP_MODEL")
    return Path(override) if override else DEFAULT_MODEL_PATH


@dataclass(frozen=True)
class RiskModel:
    features: tuple[str, ...]
    categories: tuple[str, ...]
    night_hours: frozenset[int]
    neutral_values: dict[str, float]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    threshold: float
    trained_at: str
    metrics: dict

    @classmethod
    def load(cls, path: Path | str | None = None) -> RiskModel:
        location = Path(path) if path is not None else model_path()
        if not location.exists():
            raise FileNotFoundError(
                f"Model artifact not found at {location}. Run "
                "'python scripts/train_model.py' to build it, or point FRAUD_MCP_MODEL at "
                "an existing file."
            )

        artifact = json.loads(location.read_text(encoding="utf-8"))
        return cls(
            features=tuple(artifact["features"]),
            categories=tuple(artifact["categories"]),
            night_hours=frozenset(artifact["night_hours"]),
            neutral_values=dict(artifact["neutral_values"]),
            mean=tuple(artifact["scaler"]["mean"]),
            scale=tuple(artifact["scaler"]["scale"]),
            coefficients=tuple(artifact["coefficients"]),
            intercept=float(artifact["intercept"]),
            threshold=float(artifact["threshold"]),
            trained_at=artifact["trained_at"],
            metrics=dict(artifact["metrics"]),
        )

    def score(self, vector: dict[str, float]) -> float:
        """Probability of fraud, between 0 and 1."""
        z = self.intercept
        for name, mean, scale, coefficient in zip(
            self.features, self.mean, self.scale, self.coefficients, strict=True
        ):
            z += ((vector[name] - mean) / scale) * coefficient
        return 1.0 / (1.0 + math.exp(-z))

    def drivers(self, vector: dict[str, float], limit: int = 3) -> list[tuple[str, float]]:
        """The variables that pushed the score up the most, for the audit trail.

        Each contribution is the standardised value times its coefficient, which is the
        term this variable added to the linear combination.
        """
        contributions = [
            (name, ((vector[name] - mean) / scale) * coefficient)
            for name, mean, scale, coefficient in zip(
                self.features, self.mean, self.scale, self.coefficients, strict=True
            )
        ]
        contributions.sort(key=lambda item: item[1], reverse=True)
        return [(name, round(value, 3)) for name, value in contributions[:limit] if value > 0]
