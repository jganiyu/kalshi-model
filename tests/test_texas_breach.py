import math

import pytest

from app.services.texas_breach import breach_features, first_passage_reference


def reference(**changes):
    args = dict(now_timestamp=1000.0, ticker="BTC-A", btc_price=80000.0,
                threshold=80020.0, seconds_remaining=100.0,
                margin_volatility={"reliable": True, "raw_realized_volatility": 2.0,
                                   "mvi": 8.0, "coverage": 1.0, "reversal_component": 0.5},
                source_reliable=True, price_timestamp=1000.0, max_age_seconds=20.0)
    args.update(changes)
    return breach_features(**args)


def test_reflection_reference_and_symmetry():
    result = reference()
    assert result["reference_probability"] == pytest.approx(0.31731050786291415)
    assert result["normalized_distance"] == 1.0
    assert result["btc_price"] == 80000.0 and result["threshold"] == 80020.0
    assert reference(threshold=79980.0)["reference_probability"] == result["reference_probability"]
    assert reference(threshold=80000.0)["reference_probability"] == 1.0
    assert result["calibrated"] is False and result["execution_enabled"] is False


def test_reference_time_and_distance_monotonicity():
    base = reference()["reference_probability"]
    assert reference(seconds_remaining=400)["reference_probability"] > base
    assert reference(threshold=80040)["reference_probability"] < base
    assert reference(threshold=1e300)["reference_probability"] == 0.0


@pytest.mark.parametrize("changes", [
    {"source_reliable": False}, {"price_timestamp": 970}, {"price_timestamp": 1001},
    {"threshold": float("nan")}, {"seconds_remaining": None}, {"seconds_remaining": 0},
    {"seconds_remaining": 901}, {"margin_volatility": {"reliable": False}},
    {"margin_volatility": {"reliable": True, "raw_realized_volatility": math.inf}},
])
def test_bad_evidence_never_produces_probability(changes):
    result = reference(**changes)
    assert result["status"] == "UNAVAILABLE"
    assert result["reference_probability"] is None


def test_causal_direction_and_side_orientation():
    samples = [(940 + 10 * i, 79940 + 10 * i) for i in range(7)]
    base = reference(samples=samples)
    assert base["direction_toward_threshold_dollars_per_second"] == pytest.approx(1)
    assert base["constant_trend_probability"] > base["reference_probability"]
    assert base["direction_status"] == "AVAILABLE"
    assert base["direction_window_coverage"] == 1.0
    assert base["direction_max_gap_seconds"] == 10.0
    assert reference(threshold=79980, samples=samples)["direction_toward_threshold_dollars_per_second"] == pytest.approx(-1)
    assert reference(samples=samples + [(1001, 1e6)])["direction_toward_threshold_dollars_per_second"] == pytest.approx(1)
    assert reference(samples=samples[:3])["direction_toward_threshold_dollars_per_second"] is None
    gap = reference(samples=[samples[0], samples[1], samples[-1]])
    assert gap["direction_toward_threshold_dollars_per_second"] is None
    assert gap["direction_reason"] == "Direction window contains a data gap"


def test_mvi_and_reversal_are_features_without_guessed_probability_weights():
    high = reference()
    low = reference(margin_volatility={"reliable": True, "raw_realized_volatility": 2,
                                      "mvi": 1, "reversal_component": 0})
    assert low["reference_probability"] == high["reference_probability"]
    assert low["mvi"] != high["mvi"]


def test_first_passage_trend_direction_and_extreme_numerics():
    neutral = first_passage_reference(20, 2, 100)
    assert neutral == pytest.approx(reference()["reference_probability"])
    assert first_passage_reference(20, 2, 100, 0.1) > neutral
    assert first_passage_reference(20, 2, 100, -0.1) < neutral
    assert first_passage_reference(20, 0.001, 100, 1) == 1.0
    assert first_passage_reference(20, 0.001, 100, -1) == 0.0
    assert first_passage_reference(20, 0.001, 100, 0.2) == pytest.approx(0.500099735563867, rel=1e-8)
    with pytest.raises(ValueError):
        first_passage_reference(20, 0, 100)
