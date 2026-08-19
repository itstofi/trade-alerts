import pytest

from trade_alerts.core import (
    calculate_risk,
    calculate_score,
    format_alert,
    select_best_signals,
    select_top_symbols,
)


def test_select_top_symbols_filters_and_volume_ranks_pairs():
    tickers = [
        {"symbol": "ETHUSDT", "quoteVolume": "900"},
        {"symbol": "BTCUSDT", "quoteVolume": "1000"},
        {"symbol": "USDCUSDT", "quoteVolume": "5000"},
        {"symbol": "ETHUPUSDT", "quoteVolume": "4000"},
        {"symbol": "SOLBTC", "quoteVolume": "3000"},
        {"symbol": "XRPUSDT", "quoteVolume": "50"},
    ]

    assert select_top_symbols(tickers, min_quote_volume=100, limit=2) == [
        "BTCUSDT",
        "ETHUSDT",
    ]


def test_select_top_symbols_allows_names_containing_leverage_marker_text():
    tickers = [
        {"symbol": "JUPUSDT", "quoteVolume": "300"},
        {"symbol": "SUPERUSDT", "quoteVolume": "200"},
        {"symbol": "BTCUPUSDT", "quoteVolume": "1000"},
    ]

    assert select_top_symbols(tickers, min_quote_volume=100, limit=10) == [
        "JUPUSDT",
        "SUPERUSDT",
    ]


def test_select_top_symbols_skips_non_mapping_entries():
    tickers = [
        None,
        "not-a-ticker",
        ["BTCUSDT", "1000"],
        {"symbol": "BTCUSDT", "quoteVolume": "1000"},
    ]

    assert select_top_symbols(tickers, min_quote_volume=100, limit=10) == ["BTCUSDT"]


@pytest.mark.parametrize("quote_volume", ["bad", "NaN", "Infinity", None])
def test_select_top_symbols_skips_malformed_or_non_finite_quote_volume(quote_volume):
    tickers = [
        {"symbol": "BROKENUSDT", "quoteVolume": quote_volume},
        {"symbol": "BTCUSDT", "quoteVolume": "1000"},
    ]

    assert select_top_symbols(tickers, min_quote_volume=100, limit=10) == ["BTCUSDT"]


def test_calculate_score_combines_volume_momentum_breakout_and_trend():
    row = {
        "rvol": 3.2,
        "body_pct": 0.60,
        "close_pos": 0.90,
        "close": 110.0,
        "high20": 108.0,
        "low20": 90.0,
        "ma7": 105.0,
        "ma14": 102.0,
        "ma28": 100.0,
        "ema50": 99.0,
        "body": 4.0,
        "atr": 2.0,
    }

    score, reasons = calculate_score(row, [100.0, 150.0, 200.0], "bull")

    assert score == 100
    assert reasons == [
        "rising vol x3",
        "RVOL 3.2x",
        "close near high",
        "broke above 108.00",
        "MAs stacked bull",
        "body 2.0x ATR",
    ]


def test_calculate_risk_uses_swing_atr_and_account_risk():
    row = {"close": 100.0, "low": 96.0, "high": 101.0, "atr": 2.0}

    risk = calculate_risk(
        row,
        recent_lows=[98.0, 97.0, 96.5, 97.0, 98.0],
        recent_highs=[102.0, 103.0, 102.5, 102.0, 101.5],
        direction="bull",
        account_usdt=500.0,
        risk_percent=0.02,
        max_leverage=3.0,
    )

    assert risk == pytest.approx(
        {
            "entry": 100.0,
            "stop": 95.4,
            "tp1": 106.9,
            "tp2": 113.8,
            "stop_pct": 4.6,
            "risk_amt": 10.0,
            "risk_budget": 10.0,
            "notional": 217.391304,
            "leverage": 0.434783,
        }
    )


def test_calculate_risk_reports_actual_loss_when_leverage_caps_notional():
    row = {"close": 100.0, "low": 100.0, "high": 100.0, "atr": 5.0 / 3.0}

    risk = calculate_risk(
        row,
        recent_lows=[100.0] * 5,
        recent_highs=[100.0] * 5,
        direction="bull",
        account_usdt=100.0,
        risk_percent=0.02,
        max_leverage=3.0,
    )

    assert risk["notional"] == pytest.approx(300.0)
    assert risk["risk_budget"] == pytest.approx(2.0)
    assert risk["risk_amt"] == pytest.approx(1.5)


@pytest.mark.parametrize(("low", "expected_stop_pct"), [(99.7, 0.3), (88.0, 12.0)])
def test_calculate_risk_accepts_inclusive_stop_boundaries(low, expected_stop_pct):
    row = {"close": 100.0, "low": low, "high": 100.0, "atr": 0.0}

    risk = calculate_risk(
        row,
        recent_lows=[low] * 5,
        recent_highs=[100.0] * 5,
        direction="bull",
        account_usdt=100.0,
    )

    assert risk is not None
    assert risk["stop_pct"] == pytest.approx(expected_stop_pct)


def test_calculate_score_scores_bearish_breakdown_and_trend():
    row = {
        "rvol": 3.2,
        "body_pct": 0.60,
        "close_pos": 0.10,
        "close": 90.0,
        "high20": 110.0,
        "low20": 92.0,
        "ma7": 95.0,
        "ma14": 98.0,
        "ma28": 100.0,
        "ema50": 101.0,
        "body": 4.0,
        "atr": 2.0,
    }

    score, reasons = calculate_score(row, [100.0, 150.0, 200.0], "bear")

    assert score == 100
    assert "close near low" in reasons
    assert "broke below 92" in reasons
    assert "MAs stacked bear" in reasons


def test_calculate_risk_builds_bearish_stop_and_targets():
    row = {"close": 100.0, "low": 99.0, "high": 104.0, "atr": 2.0}

    risk = calculate_risk(
        row,
        recent_lows=[98.0] * 5,
        recent_highs=[102.0, 103.0, 104.0, 103.0, 102.0],
        direction="bear",
        account_usdt=500.0,
    )

    assert risk is not None
    assert risk["stop"] == pytest.approx(104.6)
    assert risk["tp1"] == pytest.approx(93.1)
    assert risk["tp2"] == pytest.approx(86.2)


def test_format_alert_renders_signal_plan_size_and_disclaimer():
    signal = {
        "symbol": "BTCUSDT",
        "tf": "1h",
        "direction": "LONG",
        "alert_type": "Bullish Volume Breakout",
        "score": 92,
        "rvol": 3.2,
        "vol_spike_pct": 220.0,
        "breakout_lvl": 108.0,
        "reasons": ["RVOL 3.2x", "broke above 108.00"],
        "risk": {
            "entry": 110.0,
            "stop": 105.0,
            "tp1": 117.5,
            "tp2": 125.0,
            "stop_pct": 4.545,
            "risk_amt": 10.0,
            "notional": 220.0,
            "leverage": 0.44,
        },
    }

    message = format_alert(signal, account_usdt=500.0)

    assert "🚨 *BULLISH VOLUME BREAKOUT*" in message
    assert "`BTCUSDT`  ·  *1h*  ·  🟢 *LONG*  ·  Score: *92/100*" in message
    assert "· Level: `108.00`" in message
    assert "· Planned loss: 10.00 USDT (2.00% of account)" in message
    assert message.endswith("⚠️ Not financial advice. Confirm on chart before entering.")


def test_format_alert_distinguishes_capped_planned_loss_from_risk_budget():
    signal = {
        "symbol": "BTCUSDT",
        "tf": "1h",
        "direction": "LONG",
        "alert_type": "Bullish Volume Breakout",
        "score": 92,
        "rvol": 2.0,
        "vol_spike_pct": 100.0,
        "breakout_lvl": None,
        "reasons": [],
        "risk": {
            "entry": 100.0,
            "stop": 99.5,
            "tp1": 100.75,
            "tp2": 101.5,
            "stop_pct": 0.5,
            "risk_amt": 1.5,
            "risk_budget": 2.0,
            "notional": 300.0,
            "leverage": 3.0,
        },
    }

    message = format_alert(signal, account_usdt=100.0)

    assert "· Planned loss: 1.50 USDT (1.50% of account)" in message
    assert "· Risk budget: 2.00 USDT (2%)" in message


def test_select_best_signals_deduplicates_symbols_and_orders_by_score():
    candidates = [
        {"symbol": "ETHUSDT", "tf": "15m", "score": 82},
        {"symbol": "BTCUSDT", "tf": "1h", "score": 90},
        {"symbol": "ETHUSDT", "tf": "4h", "score": 95},
    ]

    assert select_best_signals(candidates, limit=2) == [
        {"symbol": "ETHUSDT", "tf": "4h", "score": 95},
        {"symbol": "BTCUSDT", "tf": "1h", "score": 90},
    ]
