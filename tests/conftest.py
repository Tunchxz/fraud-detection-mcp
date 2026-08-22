"""Shared fixtures.

This suite never touches the developer's `data/fraud.db`, and never needs the Kaggle original
dataset; it builds its own database from the versioned seed extract, once, and hands out copies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fraud_mcp.infra import seed

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "artifacts" / "model.json"
SEED_EXTRACT = seed.SEED_DIR / "transactions.csv.gz"

needs_seed = pytest.mark.skipif(
    not SEED_EXTRACT.exists(),
    reason=f"needs the versioned seed extract at {SEED_EXTRACT}",
)
needs_model = pytest.mark.skipif(
    not MODEL.exists(),
    reason="needs artifacts/model.json (versioned, or rebuilt with scripts/train_model.py)",
)


@pytest.fixture(scope="session")
def seeded(tmp_path_factory) -> seed.SeedSummary:
    """A database built from the seed extract, shared by the whole session."""
    database = tmp_path_factory.mktemp("seeded") / "fraud.db"
    return seed.build_database(database)
