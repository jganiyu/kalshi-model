from __future__ import annotations

import base64
import asyncio
import inspect
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.engine import AnalysisEngine
from app.services.market_data import ExchangeQuote, live_composite_quote
from app.services.kalshi import KalshiPublicClient
from app.services.streaming import (
    BitcoinWebSocketFeeds,
    KalshiOrderBook,
    KalshiWebSocketFeed,
    kalshi_websocket_headers,
)


def test_lifecycle_subscription_is_on_kalshi_not_bitcoin_stream() -> None:
    assert "market_lifecycle_v2" in inspect.getsource(KalshiWebSocketFeed._connection)
    assert "market_lifecycle_v2" not in inspect.getsource(
        BitcoinWebSocketFeeds._coinbase_connection
    )


def test_public_kalshi_requests_have_a_short_independent_timeout() -> None:
    async def scenario() -> None:
        observed_timeout: dict[str, float] = {}

        def responder(request: httpx.Request) -> httpx.Response:
            observed_timeout.update(request.extensions["timeout"])
            return httpx.Response(200, json={"orderbook_fp": {}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        try:
            client = KalshiPublicClient(http, "https://example.test", "KXBTC15M")
            await client.orderbook("TEST")
        finally:
            await http.aclose()

        assert observed_timeout == {
            "connect": 2.5, "read": 3.5, "write": 3.5, "pool": 1.0,
        }

    asyncio.run(scenario())


def test_kalshi_fallback_book_has_a_tighter_bounded_timeout() -> None:
    async def scenario() -> None:
        observed_timeout: dict[str, float] = {}

        def responder(request: httpx.Request) -> httpx.Response:
            observed_timeout.update(request.extensions["timeout"])
            return httpx.Response(200, json={"orderbook_fp": {}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        try:
            client = KalshiPublicClient(http, "https://example.test", "KXBTC15M")
            await client.fallback_orderbook("TEST")
        finally:
            await http.aclose()

        assert observed_timeout == {
            "connect": 0.75, "read": 1.0, "write": 1.0, "pool": 0.25,
        }

    asyncio.run(scenario())


def test_rest_book_fallback_applies_only_to_its_cached_active_ticker() -> None:
    async def scenario() -> None:
        engine = AnalysisEngine.__new__(AnalysisEngine)
        engine._update_lock = asyncio.Lock()
        engine._current_market = {
            "ticker": "ACTIVE", "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.60",
        }
        engine._last_kalshi_ws_book = 0.0
        engine._stream_status = {"Kalshi": {"connected": False}}
        engine.config = type("Config", (), {"kalshi_book_stale_seconds": 2.0})()
        engine.dashboard = {"current": {"ticker": "ACTIVE", "orderbook": {}}}
        engine._schedule_publish = lambda: None

        payload = {"orderbook_fp": {
            "yes_dollars": [["0.45", "2"]], "no_dollars": [["0.50", "3"]],
        }}
        await engine._apply_kalshi_fallback_book(
            "ACTIVE", payload, datetime.now(UTC).isoformat(), 12.0
        )

        current = engine.dashboard["current"]
        assert current["yes_bid"] == .45
        assert current["yes_ask"] == .50
        assert current["quote_source"] == "REST_FALLBACK"
        assert engine._stream_status["Kalshi"]["fallback"]["receive_ms"] == 12.0

        before = dict(current)
        await engine._apply_kalshi_fallback_book(
            "OLD", payload, datetime.now(UTC).isoformat(), 12.0
        )
        assert engine.dashboard["current"] == before

    asyncio.run(scenario())


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


def test_live_composite_discards_a_stale_venue_quote() -> None:
    now = datetime(2026, 9, 3, tzinfo=UTC)
    quotes = [
        ExchangeQuote("Coinbase", 100.0, None, None, None, 0.0, now.isoformat()),
        ExchangeQuote("Kraken", 200.0, None, None, None, 0.0, (now - timedelta(seconds=21)).isoformat()),
    ]

    composite = live_composite_quote(
        quotes, {"Coinbase", "Kraken"}, now=now, maximum_age_seconds=20,
    )

    assert composite.price == 100.0
    assert [quote.exchange for quote in composite.quotes] == ["Coinbase"]


@pytest.mark.parametrize(
    ("market", "market_state"),
    [
        ({"ticker": "NEXT", "floor_strike": None}, {"ticker": "NEXT"}),
        ({"ticker": "NEXT", "floor_strike": 100.0}, {"ticker": "PREVIOUS"}),
    ],
)
def test_live_refresh_ignores_incomplete_or_mismatched_market_transition(
    market: dict, market_state: dict
) -> None:
    engine = AnalysisEngine.__new__(AnalysisEngine)
    engine._current_market = market
    engine._market_state = market_state
    engine._latest_btc = {"price": 100.0}

    engine._refresh_cached_dashboard("2026-08-27T08:00:00+00:00")


def test_benchmark_band_and_sparse_settlement_window_block_automatic_trade() -> None:
    btc = {"exchange_count": 3, "dispersion_pct": 0.01}
    market = {
        "yes_bid": 0.54, "yes_ask": 0.55, "no_bid": 0.45, "no_ask": 0.46,
        "executable_quote_at": datetime.now(UTC).isoformat(),
    }
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


def test_data_quality_requires_current_kalshi_executable_quote() -> None:
    market = {"yes_bid": .54, "yes_ask": .55, "no_bid": .45, "no_ask": .46}
    result = AnalysisEngine._data_quality(
        {"exchange_count": 3, "dispersion_pct": .01}, market, 30.0,
        {"max_exchange_dispersion_pct": .4, "max_data_age_seconds": 20},
        reference_price=101.0, strike=100.0, benchmark_uncertainty_pct=.0001,
        settlement_window={"elapsed_seconds": 0.0, "coverage": 1.0},
    )
    assert result == {
        "reliable": False,
        "reason": "the Kalshi executable quote timestamp is unavailable",
    }

    market["executable_quote_at"] = (datetime.now(UTC) - timedelta(seconds=21)).isoformat()
    stale = AnalysisEngine._data_quality(
        {"exchange_count": 3, "dispersion_pct": .01}, market, 30.0,
        {"max_exchange_dispersion_pct": .4, "max_data_age_seconds": 20},
        reference_price=101.0, strike=100.0, benchmark_uncertainty_pct=.0001,
        settlement_window={"elapsed_seconds": 0.0, "coverage": 1.0},
    )
    assert stale["reason"] == "the Kalshi executable quote is stale"
