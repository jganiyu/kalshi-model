from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.engine import AnalysisEngine
from app.services.market_data import ExchangeQuote, live_composite_quote
from app.services.streaming import KalshiOrderBook, kalshi_websocket_headers


def test_kalshi_websocket_headers_sign_expected_message(tmp_path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "kalshi.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    headers = kalshi_websocket_headers(
        "test-key-id", key_path, timestamp_ms=1_700_000_000_000
    )

    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    private_key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/ws/v2",
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_orderbook_snapshot_and_delta_produce_executable_prices() -> None:
    book = KalshiOrderBook()
    metrics = book.apply(
        {
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "TEST",
                "yes_dollars_fp": [["0.40", "10.00"], ["0.39", "3.00"]],
                "no_dollars_fp": [["0.55", "8.00"]],
            },
        }
    )

    assert metrics is not None
    assert metrics["yes_bids"][0] == (0.4, 10.0)
    assert metrics["yes_asks"][0] == pytest.approx((0.45, 8.0))

    metrics = book.apply(
        {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "TEST",
                "side": "yes",
                "price_dollars": "0.40",
                "delta_fp": "-10.00",
            },
        }
    )

    assert metrics is not None
    assert metrics["yes_bids"][0] == (0.39, 3.0)


def test_orderbook_removes_floating_point_quantity_dust() -> None:
    book = KalshiOrderBook()
    book.apply(
        {
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "TEST",
                "yes_dollars_fp": [["0.90", "0.10"], ["0.40", "1.00"]],
                "no_dollars_fp": [],
            },
        }
    )
    book.apply(
        {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "TEST",
                "side": "yes",
                "price_dollars": "0.90",
                "delta_fp": "0.20",
            },
        }
    )
    metrics = book.apply(
        {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "TEST",
                "side": "yes",
                "price_dollars": "0.90",
                "delta_fp": "-0.30",
            },
        }
    )

    assert metrics is not None
    assert metrics["yes_bids"][0] == (0.4, 1.0)


def test_ticker_update_reprices_both_sides() -> None:
    state = AnalysisEngine._ticker_market_state(
        {
            "ticker": "TEST",
            "yes_bid_dollars": "0.47",
            "yes_ask_dollars": "0.50",
            "yes_bid_size_fp": "25.00",
            "yes_ask_size_fp": "18.00",
            "volume_fp": "300.00",
            "open_interest_fp": "200.00",
        },
        {"yes_bid": 0.46, "yes_ask": 0.49, "imbalance": 0.2},
    )

    assert state["yes_bid"] == 0.47
    assert state["yes_ask"] == 0.50
    assert state["no_bid"] == 0.50
    assert state["no_ask"] == 0.53
    assert state["rapid_repricing"] == pytest.approx(0.01)
    assert state["imbalance"] == 0.2


def test_live_composite_is_not_pinned_by_rest_only_quote() -> None:
    quotes = [
        ExchangeQuote("Coinbase", 100.0, None, None, None, 0.0),
        ExchangeQuote("Kraken", 104.0, None, None, None, 0.0),
        ExchangeQuote("Bitstamp", 101.0, None, None, None, 10.0),
    ]

    composite = live_composite_quote(quotes, {"Coinbase", "Kraken"})

    assert composite.price == 102.0
    assert composite.dispersion_pct == pytest.approx((4.0 / 101.0) * 100)
    assert len(composite.quotes) == 3


def test_benchmark_band_and_sparse_settlement_window_block_automatic_trade() -> None:
    btc = {"exchange_count": 3, "dispersion_pct": 0.01}
    market = {"yes_bid": 0.54, "yes_ask": 0.55, "no_bid": 0.45, "no_ask": 0.46}
    settings = {"max_exchange_dispersion_pct": 0.4}

    inside_band = AnalysisEngine._data_quality(
        btc,
        market,
        30.0,
        settings,
        reference_price=100.01,
        strike=100.0,
        benchmark_uncertainty_pct=0.00015,
        settlement_window={"elapsed_seconds": 30.0, "coverage": 1.0},
    )
    sparse_window = AnalysisEngine._data_quality(
        btc,
        market,
        30.0,
        settings,
        reference_price=101.0,
        strike=100.0,
        benchmark_uncertainty_pct=0.00015,
        settlement_window={"elapsed_seconds": 30.0, "coverage": 0.25},
    )

    assert inside_band["reliable"] is True
    assert inside_band["trade_allowed"] is False
    assert inside_band["reason_code"] == "BENCHMARK_UNCERTAINTY"
    assert sparse_window["reason_code"] == "SETTLEMENT_WINDOW_INCOMPLETE"
