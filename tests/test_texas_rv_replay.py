from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from app.db import Database
from app.services.texas_rv_replay import (
    _fill_pnl, _point_crossing, _summary, _sweep_loss_minimization, bucket,
    replay_texas_rv, rv15_at,
)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def test_rv15_is_causal_and_gap_safe() -> None:
    decision = 1_700_001_230  # 30 seconds into minute ending at 1_700_001_200
    end = decision // 60 * 60 - 60
    candles = {end - index * 60: 100 * 1.001 ** (15 - index) for index in range(16)}
    value = rv15_at(candles, _iso(decision))
    assert value["available"]
    assert value["window_end_epoch"] == end
    assert value["rv15_pct"] == pytest.approx(100 * math.sqrt(15 * math.log(1.001) ** 2))
    # A huge future candle cannot influence the historical decision.
    candles[end + 60] = 1_000_000
    assert rv15_at(candles, _iso(decision))["rv15_pct"] == pytest.approx(value["rv15_pct"])
    candles.pop(end - 8 * 60)
    assert rv15_at(candles, _iso(decision))["reason"] == "missing candle window"


def test_buckets_include_boundaries() -> None:
    assert bucket(.199999) == "<0.20%"
    assert bucket(.20) == "0.20–<0.40%"
    assert bucket(.40) == "0.40–<0.80%"
    assert bucket(.80) == "≥0.80%"


def test_replay_aggregates_exact_partial_buy_fills_fifo_and_never_writes(tmp_path) -> None:
    db = Database(tmp_path / "replay.sqlite")
    db.initialize()
    decision = 1_700_002_030
    end = decision // 60 * 60 - 60
    db.execute(
        """INSERT INTO texas_holdem_rounds(environment,ticker,strategy,threshold,side,status,entry_price_cap,
           flop_target,turn_target,river_target,river_stop,created_at,updated_at,exit_reason)
           VALUES ('LIVE','KX','TEXAS_HOLDEM_2_0',100,'YES','EXITED',.5,.6,.5,.95,.6,?,?, 'TEXAS_FLOP_TARGET')""",
        (_iso(decision), _iso(decision)),
    )
    round_id = db.fetch_one("SELECT id FROM texas_holdem_rounds")["id"]
    db.execute(
        """INSERT INTO texas_holdem_attempts(round_id,attempt_number,observed_at,side,status,broker_client_order_id,evidence_json)
           VALUES (?,?,?,?,?,?, '{}')""", (round_id, 1, _iso(decision), "YES", "FILLED", "client-buy"),
    )
    db.execute(
        """INSERT INTO broker_order_intents(mode,client_order_id,ticker,side,action,requested_contracts,limit_price,status,strategy,source,created_at,updated_at)
           VALUES ('LIVE','client-buy','KX','YES','BUY',5,.4,'FILLED','TEXAS_HOLDEM_2_0','texas_entry',?,?)""",
        (_iso(decision), _iso(decision)),
    )
    for fill_id, qty, price, fee in (("a", 2, .4, .02), ("b", 3, .5, .03)):
        db.execute(
            """INSERT INTO broker_fills(mode,fill_id,client_order_id,ticker,side,action,contracts,price,fee,strategy,filled_at)
               VALUES ('LIVE',?,'client-buy','KX','YES','BUY',?,?,?,'TEXAS_HOLDEM_2_0',?)""",
            (fill_id, qty, price, fee, _iso(decision + 10)),
        )
    # The same economic unit has an unclassified/manual sell.  It must be
    # included, but may not sell more than actual FIFO purchases.
    db.execute(
        """INSERT INTO broker_fills(mode,fill_id,ticker,side,action,contracts,price,fee,strategy,filled_at)
           VALUES ('LIVE','manual-sell','KX','YES','SELL',4,.6,.04,NULL,?)""", (_iso(decision + 20),),
    )
    for index in range(16):
        epoch = end - (15 - index) * 60
        db.execute(
            """INSERT INTO coinbase_realized_volatility_candles(source,product,granularity_seconds,minute_epoch,close,fetched_at)
               VALUES ('Coinbase','BTC-USD',60,?,?,?)""", (epoch, 100 * 1.001 ** index, _iso(decision)),
        )
    report = replay_texas_rv(db)
    row = report["rounds"][0]
    assert report["denominators"]["attempts"] == 1
    assert sum(value["attempt_rounds"] for value in report["by_bucket"].values()) == 1
    assert row["pnl"]["buy_contracts"] == 5
    assert row["pnl"]["sell_contracts"] == 4
    # Four sold at .6 less .04 fee.  The remaining buy lot stays explicitly
    # open: net round P/L is not invented, while FIFO realized P/L is visible.
    assert row["pnl"]["remaining_contracts"] == 1
    assert row["pnl"]["net_pnl"] is None
    assert row["pnl"]["realized_pnl"] == pytest.approx(.52)
    assert row["manual_or_unclassified_sells"] is True
    assert db.fetch_one("SELECT COUNT(*) count FROM coinbase_realized_volatility_candles")["count"] == 16


def test_fifo_never_uses_a_later_buy_to_cover_an_earlier_sell() -> None:
    t = 1_700_100_000
    pnl = _fill_pnl(
        [{"id": 1, "filled_at": _iso(t), "contracts": 1, "price": .4, "fee": 0},
         {"id": 3, "filled_at": _iso(t + 20), "contracts": 1, "price": .9, "fee": 0}],
        [{"id": 2, "filled_at": _iso(t + 10), "contracts": 2, "price": .6, "fee": 0}],
        None, "YES",
    )
    assert pnl["unmatched_sell_contracts"] == 1
    assert pnl["remaining_contracts"] == 1
    assert pnl["open_cost_basis"] == pytest.approx(.9)
    assert pnl["net_pnl"] is None


def test_fifo_preserves_fractional_timestamp_order_within_one_second() -> None:
    """A later buy at .900 may not cover a sell at .100 in the same second."""
    pnl = _fill_pnl(
        [{"id": 1, "filled_at": "2023-11-16T08:00:00.900000Z", "contracts": 1, "price": .4, "fee": 0}],
        [{"id": 2, "filled_at": "2023-11-16T08:00:00.100000Z", "contracts": 1, "price": .6, "fee": 0}],
        None, "YES",
    )
    assert pnl["unmatched_sell_contracts"] == 1
    assert pnl["remaining_contracts"] == 1
    assert pnl["open_cost_basis"] == pytest.approx(.4)


def test_replay_labels_first_attempt_and_first_funded_bucket_views(tmp_path) -> None:
    db = Database(tmp_path / "bucket-labels.sqlite")
    db.initialize()
    report = replay_texas_rv(db)
    assert report["bucket_basis"]["by_bucket"].startswith("first submitted")
    assert report["bucket_basis"]["by_first_funded_attempt_bucket"].startswith("first exact-linked")
    assert set(report["by_first_funded_attempt_bucket"]) == set(report["by_bucket"])


def test_unknown_settlement_and_settlement_fee_semantics_are_not_invented() -> None:
    buy = [{"id": 1, "filled_at": _iso(1_700_100_000), "contracts": 1, "price": .4, "fee": .1}]
    unknown = _fill_pnl(buy, [], {"market_result": "PENDING", "fees": .1}, "YES")
    assert unknown["settlement_state"] == "UNRESOLVED"
    assert unknown["net_pnl"] is None
    settled = _fill_pnl(buy, [], {"market_result": "YES", "fees": .1}, "YES")
    # Fill fee is exact; settlement summary fees are not an additional fill fee.
    assert settled["settlement_payout"] == 1
    assert settled["settlement_fee"] == 0
    assert settled["net_pnl"] == pytest.approx(.5)


def test_manual_sell_is_not_attributed_when_other_strategy_may_own_position(tmp_path) -> None:
    db = Database(tmp_path / "ambiguous.sqlite")
    db.initialize()
    decision = 1_700_200_030
    end = decision // 60 * 60 - 60
    db.execute(
        """INSERT INTO texas_holdem_rounds(environment,ticker,strategy,threshold,side,status,entry_price_cap,
           flop_target,turn_target,river_target,river_stop,created_at,updated_at)
           VALUES ('LIVE','KX-AMB','TEXAS_HOLDEM_2_0',100,'YES','EXITED',.5,.6,.5,.95,.6,?,?)""",
        (_iso(decision), _iso(decision)),
    )
    round_id = db.fetch_one("SELECT id FROM texas_holdem_rounds")["id"]
    db.execute(
        """INSERT INTO texas_holdem_attempts(round_id,attempt_number,observed_at,side,status,broker_client_order_id,evidence_json)
           VALUES (?,?,?,?,?,?, '{}')""", (round_id, 1, _iso(decision), "YES", "FILLED", "tx-buy"),
    )
    db.execute(
        """INSERT INTO broker_order_intents(mode,client_order_id,ticker,side,action,requested_contracts,limit_price,status,strategy,source,created_at,updated_at)
           VALUES ('LIVE','tx-buy','KX-AMB','YES','BUY',1,.4,'FILLED','TEXAS_HOLDEM_2_0','texas_entry',?,?)""",
        (_iso(decision), _iso(decision)),
    )
    for fill_id, client_id, strategy, action, qty, price, at in (
        ("tx-buy", "tx-buy", "TEXAS_HOLDEM_2_0", "BUY", 1, .4, decision + 10),
        ("other-buy", None, "STANDARD_EDGE", "BUY", 1, .3, decision + 15),
        ("manual-sell", None, None, "SELL", 1, .6, decision + 20),
    ):
        db.execute(
            """INSERT INTO broker_fills(mode,fill_id,client_order_id,ticker,side,action,contracts,price,fee,strategy,filled_at)
               VALUES ('LIVE',?,?, 'KX-AMB','YES',?,?,?,0,?,?)""",
            (fill_id, client_id, action, qty, price, strategy, _iso(at)),
        )
    for index in range(16):
        epoch = end - (15 - index) * 60
        db.execute("""INSERT INTO coinbase_realized_volatility_candles(source,product,granularity_seconds,minute_epoch,close,fetched_at)
                    VALUES ('Coinbase','BTC-USD',60,?,?,?)""", (epoch, 100 + index, _iso(decision)))
    row = replay_texas_rv(db)["rounds"][0]
    assert row["pnl"]["sell_contracts"] == 0
    assert row["pnl"]["net_pnl"] is None
    assert row["sell_attribution_ambiguities"][0]["reason"] == "manual sell ownership ambiguous"


def test_crossing_is_bounded_at_market_close_and_summary_excludes_unknown_pnl() -> None:
    opened, closed = 1_700_300_000, 1_700_300_900
    crossing = _point_crossing(
        [{"observed_at": _iso(opened + 1), "btc_proxy": 99, "data_reliable": 1},
         {"observed_at": _iso(closed + 1), "btc_proxy": 101, "data_reliable": 1}],
        "YES", 100, _iso(opened), _iso(closed),
    )
    assert crossing["observed_touch"] is False
    summary = _summary([{
        "first_attempt": {"rv": {"bucket": "0.20–<0.40%"}},
        "pnl": {"buy_contracts": 1, "net_pnl": None},
        "crossing": {"observed_touch": None}, "accounting_status": "RESOLVED_OR_PARTIAL",
        "actual_exit_reason": "TEXAS_FLOP_TARGET", "confirmed_texas_exit_sell_contracts": 0,
    }])
    value = summary["0.20–<0.40%"]
    assert value["resolved_net_pnl_rounds"] == 0
    assert value["unresolved_net_pnl_rounds"] == 1
    assert value["net_pnl"] == 0
    assert value["actual_target_exits"] == 0
    paper = _summary([{
        "first_attempt": {"rv": {"bucket": "0.20–<0.40%"}},
        "pnl": {"buy_contracts": 0, "net_pnl": None},
        "crossing": {"observed_touch": None},
        "accounting_status": "UNAVAILABLE_PAPER_NO_BROKER_FILLS",
    }])["0.20–<0.40%"]
    assert paper["filled_rounds"] == 0
    assert paper["paper_broker_fill_unavailable_rounds"] == 1


def test_loss_minimization_sweep_is_causal_gap_safe_and_marks_remaining_at_bid() -> None:
    started = 1_700_400_000
    points = [
        {"id": index, "observed_at": _iso(started + index * 10), "data_reliable": 1,
         "btc_proxy": 940 - index, "threshold": 1_000, "yes_bid": .35, "no_bid": .64}
        for index in range(31)
    ]
    base = {
        "round_id": 1, "side": "YES", "threshold": 1_000,
        "first_fill_at": _iso(started), "market_close_time": _iso(started + 900),
        "points": points,
        "buy_fills": [{"id": 1, "filled_at": _iso(started), "contracts": 2, "price": .4, "fee": .02}],
        "sells": [], "sell_attribution_ambiguous": False, "actual_net_pnl": -.82,
    }
    sweep = _sweep_loss_minimization([base], checkpoints=(300,), buffers=(0, 35, 75))
    # At five minutes the held YES has never touched $1,000 and is $90 below it.
    variant = sweep["variants"]["300s/$75"]
    assert variant["qualified_rounds"] == 1
    row = variant["rounds"][0]
    assert row["remaining_contracts"] == 2
    assert row["recorded_bid"] == .35
    assert row["estimated_net_pnl"] == pytest.approx(2 * .35 - .0319 - .82)
    assert variant["synthetic_mark_to_bid_net_pnl"] > variant["actual_net_pnl_for_same_rounds"]
    # A threshold touch makes the rule ineligible even if the final point is far away.
    touched = dict(base, points=[*points, {"id": 99, "observed_at": _iso(started + 150), "data_reliable": 1,
                                   "btc_proxy": 1_000, "yes_bid": .5, "no_bid": .49}])
    assert _sweep_loss_minimization([touched], checkpoints=(300,), buffers=(0,))["variants"]["300s/$0"]["qualified_rounds"] == 0
    # A gap is excluded; it cannot become a synthetic no-breach result.
    gapped = dict(base, points=[points[0], points[-1]])
    assert _sweep_loss_minimization([gapped], checkpoints=(300,), buffers=(0,))["variants"]["300s/$0"]["excluded"]["review_gap_exceeds_20s"] == 1
