"""Pure, deterministic market-scanner logic."""

import math
from collections.abc import Mapping

STABLECOINS = {"USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "USDD", "FRAX"}
# Exact Binance leveraged-token bases. Substring matching is unsafe because spot
# assets such as JUP and SUPER legitimately contain the same marker text.
BINANCE_LEVERAGED_TOKENS = {
    "1INCHDOWN",
    "1INCHUP",
    "ADADOWN",
    "ADAUP",
    "AAVEDOWN",
    "AAVEUP",
    "BNBDOWN",
    "BNBUP",
    "BTCDOWN",
    "BTCUP",
    "DOTDOWN",
    "DOTUP",
    "ETHDOWN",
    "ETHUP",
    "FILDOWN",
    "FILUP",
    "LINKDOWN",
    "LINKUP",
    "LTCDOWN",
    "LTCUP",
    "SUSHIDOWN",
    "SUSHIUP",
    "SXPDOWN",
    "SXPUP",
    "TRXDOWN",
    "TRXUP",
    "UNIDOWN",
    "UNIUP",
    "XLMDOWN",
    "XLMUP",
    "XRPDOWN",
    "XRPUP",
    "XTZDOWN",
    "XTZUP",
    "YFIDOWN",
    "YFIUP",
}


def select_top_symbols(tickers, min_quote_volume, limit):
    """Return eligible USDT symbols ordered by descending 24-hour quote volume."""
    ranked = []
    for ticker in tickers:
        if not isinstance(ticker, Mapping):
            continue
        symbol = ticker.get("symbol")
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLECOINS or base in BINANCE_LEVERAGED_TOKENS:
            continue
        try:
            quote_volume = float(ticker.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(quote_volume):
            continue
        if quote_volume >= min_quote_volume:
            ranked.append((symbol, quote_volume))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in ranked[:limit]]


def format_price(price):
    """Format a market price without insignificant trailing zeroes."""
    if price < 0.001:
        return f"{price:.8f}".rstrip("0")
    if price < 1:
        return f"{price:.6f}".rstrip("0")
    if price < 100:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    return f"{price:,.2f}"


def calculate_score(row, recent_volumes, direction):
    """Score volume, candle momentum, breakout, trend, and ATR expansion."""
    points = 0
    reasons = []

    rvol = row["rvol"]
    if rvol >= 4.0:
        volume_points = 30
    elif rvol >= 3.0:
        volume_points = 25
    elif rvol >= 2.0:
        volume_points = 20
    elif rvol >= 1.5:
        volume_points = 12
    elif rvol >= 1.2:
        volume_points = 5
    else:
        volume_points = 0

    if len(recent_volumes) >= 3 and recent_volumes[-3] < recent_volumes[-2] < recent_volumes[-1]:
        volume_points = min(30, volume_points + 5)
        reasons.append("rising vol x3")
    if volume_points >= 12:
        reasons.append(f"RVOL {rvol:.1f}x")
    points += volume_points

    body_percent = row["body_pct"]
    if body_percent >= 0.70:
        momentum_points = 25
    elif body_percent >= 0.55:
        momentum_points = 20
    elif body_percent >= 0.40:
        momentum_points = 14
    elif body_percent >= 0.25:
        momentum_points = 7
    else:
        momentum_points = 0

    close_position = row["close_pos"]
    if direction == "bull" and close_position >= 0.80:
        momentum_points = min(25, momentum_points + 5)
        reasons.append("close near high")
    elif direction == "bear" and close_position <= 0.20:
        momentum_points = min(25, momentum_points + 5)
        reasons.append("close near low")
    points += momentum_points

    price = row["close"]
    if direction == "bull" and price > row["high20"]:
        breakout_points = 20
        reasons.append(f"broke above {format_price(row['high20'])}")
    elif direction == "bear" and price < row["low20"]:
        breakout_points = 20
        reasons.append(f"broke below {format_price(row['low20'])}")
    elif direction == "bull" and price > row["ma28"] and price > row["ema50"]:
        breakout_points = 10
    elif direction == "bear" and price < row["ma28"] and price < row["ema50"]:
        breakout_points = 10
    else:
        breakout_points = 0
    points += breakout_points

    trend_points = 0
    if direction == "bull":
        if price > row["ma7"] > row["ma14"] > row["ma28"]:
            trend_points = 15
            reasons.append("MAs stacked bull")
        elif price > row["ma14"] > row["ma28"]:
            trend_points = 10
        elif price > row["ma28"]:
            trend_points = 5
    else:
        if price < row["ma7"] < row["ma14"] < row["ma28"]:
            trend_points = 15
            reasons.append("MAs stacked bear")
        elif price < row["ma14"] < row["ma28"]:
            trend_points = 10
        elif price < row["ma28"]:
            trend_points = 5
    points += trend_points

    if row["atr"] > 0:
        atr_ratio = row["body"] / row["atr"]
        if atr_ratio >= 2.0:
            points += 10
            reasons.append(f"body {atr_ratio:.1f}x ATR")
        elif atr_ratio >= 1.5:
            points += 7
        elif atr_ratio >= 1.0:
            points += 4

    return min(100, points), reasons


def calculate_risk(
    row,
    recent_lows,
    recent_highs,
    direction,
    account_usdt,
    risk_percent=0.02,
    max_leverage=3.0,
):
    """Build a swing/ATR stop and fixed-R targets with risk-based sizing."""
    price = row["close"]
    atr = row["atr"]
    if direction == "bull":
        stop = min(min(recent_lows), row["low"]) - atr * 0.3
        risk_unit = price - stop
        tp1 = price + risk_unit * 1.5
        tp2 = price + risk_unit * 3.0
    else:
        stop = max(max(recent_highs), row["high"]) + atr * 0.3
        risk_unit = stop - price
        tp1 = price - risk_unit * 1.5
        tp2 = price - risk_unit * 3.0

    if risk_unit <= 0:
        return None
    stop_fraction = risk_unit / price
    if (stop_fraction < 0.003 and not math.isclose(stop_fraction, 0.003)) or (
        stop_fraction > 0.12 and not math.isclose(stop_fraction, 0.12)
    ):
        return None

    risk_budget = account_usdt * risk_percent
    notional = risk_budget / stop_fraction
    leverage = min(notional / account_usdt, max_leverage)
    if leverage == max_leverage:
        notional = account_usdt * leverage
    risk_amount = notional * stop_fraction

    return {
        "entry": price,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "stop_pct": stop_fraction * 100,
        "risk_amt": risk_amount,
        "risk_budget": risk_budget,
        "notional": notional,
        "leverage": leverage,
    }


def format_alert(signal, account_usdt, risk_percent=0.02, max_leverage=3.0):
    """Render a qualified signal as a Telegram Markdown alert."""
    risk = signal["risk"]
    tag = "🟢" if signal["direction"] == "LONG" else "🔴"
    reasons = " · ".join(signal["reasons"]) or "volume + momentum"
    level_line = (
        f"· Level: `{format_price(signal['breakout_lvl'])}`\n" if signal["breakout_lvl"] else ""
    )
    risk_label = risk_percent * 100
    actual_risk_label = risk["risk_amt"] / account_usdt * 100
    risk_budget = risk.get("risk_budget", account_usdt * risk_percent)
    budget_line = (
        f"· Risk budget: {risk_budget:.2f} USDT ({risk_label:.0f}%)\n"
        if not math.isclose(risk["risk_amt"], risk_budget)
        else ""
    )

    return (
        f"🚨 *{signal['alert_type'].upper()}*\n"
        f"`{signal['symbol']}`  ·  *{signal['tf']}*  ·  {tag} "
        f"*{signal['direction']}*  ·  Score: *{signal['score']}/100*\n\n"
        "📊 *SIGNAL*\n"
        f"· Price: `{format_price(risk['entry'])}`\n"
        f"· Vol spike: `+{signal['vol_spike_pct']:.0f}%` above 20-candle avg\n"
        f"· RVOL: `{signal['rvol']:.1f}x`\n"
        f"{level_line}"
        f"· Why: _{reasons}_\n\n"
        "🎯 *PLAN*\n"
        f"· Entry:       `{format_price(risk['entry'])}`\n"
        f"· Stop:        `{format_price(risk['stop'])}`  (−{risk['stop_pct']:.2f}%)\n"
        f"· TP1 (1.5R):  `{format_price(risk['tp1'])}`  — close 50%, stop→BE\n"
        f"· TP2 (3R):    `{format_price(risk['tp2'])}`  — trail rest\n"
        "· R:R:         1.5:1 / 3:1\n\n"
        "💰 *SIZE*\n"
        f"· Account: {account_usdt:.0f} USDT\n"
        f"· Planned loss: {risk['risk_amt']:.2f} USDT ({actual_risk_label:.2f}% of account)\n"
        f"{budget_line}"
        f"· Notional: {risk['notional']:.2f} USDT\n"
        f"· Leverage: {risk['leverage']:.2f}x (≤{max_leverage:g}x)\n\n"
        "⚠️ Not financial advice. Confirm on chart before entering."
    )


def select_best_signals(candidates, limit):
    """Keep the highest score per symbol, then return the run's top signals."""
    best_by_symbol = {}
    for candidate in candidates:
        symbol = candidate["symbol"]
        if symbol not in best_by_symbol or candidate["score"] > best_by_symbol[symbol]["score"]:
            best_by_symbol[symbol] = candidate
    ranked = sorted(best_by_symbol.values(), key=lambda item: item["score"], reverse=True)
    return ranked[:limit]
