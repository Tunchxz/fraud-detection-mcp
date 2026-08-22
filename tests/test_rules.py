"""The explicit fraud rules.

Every rule gets a case that fires it and a case that does not. The rules are pure functions
over entities, so no database and no server are involved.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from fraud_mcp.domain import rules
from fraud_mcp.domain.models import Account, CardHistory, Transaction

NOON = datetime(2026, 5, 20, 13, 0, 0)  # a Wednesday, outside the night window

# Two points far enough apart that fourteen minutes between them is impossible.
HOME = (47.8, -90.2)
ABROAD = (14.6349, -90.5069)

ACCOUNT = Account(
    account_id="4471",
    holder_name="Test Holder",
    city="Hovland",
    state="MN",
    lat=HOME[0],
    lon=HOME[1],
    city_pop=1200,
    dob=date(1980, 3, 9),
    gender="M",
    job="Engineer",
)


def transaction(
    amount: float = 50.0,
    ts: datetime = NOON,
    category: str = "food_dining",
    channel: str = "in_person",
    country: str = "US",
    location: tuple[float, float] = HOME,
    txn_id: str = "txn_current",
) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        account_id="4471",
        card_id="card_4471",
        ts=ts,
        amount=amount,
        merchant="Test Merchant",
        category=category,
        channel=channel,
        country=country,
        merch_lat=location[0],
        merch_lon=location[1],
    )


def history(previous: Transaction | None, count: int = 100, mean: float | None = 60.0) -> CardHistory:
    return CardHistory(prior_count=count, prior_mean=mean, previous=previous)


def fired(hits: list[rules.RuleHit]) -> set[str]:
    return {hit.rule for hit in hits}


# --- Geovelocity -------------------------------------------------------------------


def test_impossible_geovelocity_fires_between_distant_card_present_purchases():
    previous = transaction(ts=NOON - timedelta(minutes=14), txn_id="txn_previous")
    current = transaction(country="GT", location=ABROAD)

    hits = rules.evaluate(current, ACCOUNT, history(previous))
    hit = next(item for item in hits if item.rule == "impossible_geovelocity")

    assert hit.severity == "high"
    assert hit.evidence["implied_speed_kmh"] > rules.IMPOSSIBLE_SPEED_KMH
    assert hit.evidence["previous_transaction_id"] == "txn_previous"
    assert "GT" in hit.reason


def test_geovelocity_ignores_online_purchases():
    # Buying online from anywhere implies no travel, so the pair says nothing.
    previous = transaction(ts=NOON - timedelta(minutes=14), channel="online", category="misc_net")
    current = transaction(country="GT", location=ABROAD)

    assert "impossible_geovelocity" not in fired(rules.evaluate(current, ACCOUNT, history(previous)))


def test_geovelocity_accepts_a_reachable_distance():
    previous = transaction(ts=NOON - timedelta(hours=8), txn_id="txn_previous")
    current = transaction(country="GT", location=ABROAD)

    hits = fired(rules.evaluate(current, ACCOUNT, history(previous)))
    assert "impossible_geovelocity" not in hits


def test_geovelocity_needs_a_previous_transaction():
    assert "impossible_geovelocity" not in fired(
        rules.evaluate(transaction(), ACCOUNT, history(None, count=0, mean=None))
    )


# --- Amount against the profile ----------------------------------------------------


def test_amount_vs_profile_fires_far_above_the_historical_average():
    current = transaction(amount=60.0 * rules.AMOUNT_RATIO_ALERT)
    hit = next(
        item
        for item in rules.evaluate(current, ACCOUNT, history(None))
        if item.rule == "amount_vs_profile"
    )
    assert hit.severity == "high"
    assert hit.evidence["ratio"] == pytest.approx(rules.AMOUNT_RATIO_ALERT)


def test_amount_vs_profile_stays_quiet_within_the_profile():
    assert "amount_vs_profile" not in fired(
        rules.evaluate(transaction(amount=90.0), ACCOUNT, history(None))
    )


def test_amount_vs_profile_needs_history():
    current = transaction(amount=5000.0)
    assert "amount_vs_profile" not in fired(
        rules.evaluate(current, ACCOUNT, history(None, count=0, mean=None))
    )


# --- Night window ------------------------------------------------------------------


@pytest.mark.parametrize("hour", sorted(rules.NIGHT_HOURS))
def test_night_window_fires_inside_the_window(hour):
    current = transaction(ts=NOON.replace(hour=hour))
    assert "night_window" in fired(rules.evaluate(current, ACCOUNT, history(None)))


@pytest.mark.parametrize("hour", [4, 10, 15, 21])
def test_night_window_stays_quiet_outside_the_window(hour):
    current = transaction(ts=NOON.replace(hour=hour))
    assert "night_window" not in fired(rules.evaluate(current, ACCOUNT, history(None)))


# --- Category, succession and amount -----------------------------------------------


@pytest.mark.parametrize("category", rules.HIGH_RISK_CATEGORIES)
def test_high_risk_category_fires(category):
    channel = "online" if category.endswith("_net") else "in_person"
    current = transaction(category=category, channel=channel)
    assert "high_risk_category" in fired(rules.evaluate(current, ACCOUNT, history(None)))


def test_low_risk_category_stays_quiet():
    current = transaction(category="health_fitness")
    assert "high_risk_category" not in fired(rules.evaluate(current, ACCOUNT, history(None)))


def test_rapid_succession_fires_within_minutes():
    previous = transaction(ts=NOON - timedelta(minutes=4), txn_id="txn_previous")
    hit = next(
        item
        for item in rules.evaluate(transaction(), ACCOUNT, history(previous))
        if item.rule == "rapid_succession"
    )
    assert hit.evidence["minutes_apart"] == pytest.approx(4.0)


def test_rapid_succession_stays_quiet_after_a_normal_gap():
    previous = transaction(ts=NOON - timedelta(hours=3), txn_id="txn_previous")
    assert "rapid_succession" not in fired(rules.evaluate(transaction(), ACCOUNT, history(previous)))


def test_high_amount_fires_at_the_threshold():
    current = transaction(amount=rules.HIGH_AMOUNT_USD)
    assert "high_amount" in fired(rules.evaluate(current, ACCOUNT, history(None)))


def test_high_amount_stays_quiet_below_the_threshold():
    current = transaction(amount=rules.HIGH_AMOUNT_USD - 0.01)
    assert "high_amount" not in fired(rules.evaluate(current, ACCOUNT, history(None)))


# --- Ordering and quiet cases ------------------------------------------------------


def test_severe_rules_come_first():
    previous = transaction(ts=NOON - timedelta(minutes=14), txn_id="txn_previous")
    current = transaction(amount=1200.0, ts=NOON.replace(hour=23), country="GT", location=ABROAD)

    hits = rules.evaluate(current, ACCOUNT, history(previous))
    severities = [hit.severity for hit in hits]

    assert severities == sorted(severities, key=lambda value: 0 if value == "high" else 1)
    assert hits[0].severity == "high"


def test_an_ordinary_purchase_fires_nothing():
    previous = transaction(ts=NOON - timedelta(hours=5), txn_id="txn_previous")
    current = transaction(amount=42.0, category="home")

    assert rules.evaluate(current, ACCOUNT, history(previous)) == []
