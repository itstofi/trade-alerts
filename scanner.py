#!/usr/bin/env python3
# Market mover scanner — top 200 Binance USDT pairs
# Alerts only on real movement: volume spike + strong candle + scoring system
# Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_USDT

import math
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from trade_alerts.core import (
    calculate_risk,
    calculate_score,
    format_alert,
    select_best_signals,
    select_top_symbols,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TIMEFRAMES = {"15m": "15m", "1h": "1h", "4h": "4h"}
MIN_SCORE = 80  # alert threshold 0-100
VOLUME_SPIKE_MULT = 2.0  # min RVOL vs 20-candle avg (early filter)
MAX_ALERTS_PER_RUN = 5  # send only top N by score per scan
HEARTBEAT_HOUR = 8  # UTC hour for daily alive ping (0–23)
CANDLE_LIMIT = 100  # candles fetched per request
ACCOUNT_USDT = os.getenv("ACCOUNT_USDT", "100")
RISK_PCT = 0.02
MAX_LEVERAGE = 3.0
API_SLEEP = 0.25  # seconds between API calls
REQUEST_TIMEOUT = 15

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Updated at startup by detect_exchange() — do not set these manually
BINANCE_BASE = "https://api.binance.com"
MIN_24H_QUOTE_VOL = 5_000_000
TOP_N_COINS = 200

# Ordered list of (base_url, min_vol, max_coins) — first accessible one wins
_EXCHANGE_OPTS = [
    ("https://api.binance.com", 5_000_000, 200),  # global Binance — 600+ pairs
    ("https://api.binance.us", 25_000, 50),  # US fallback — ~15 active pairs
]


def parse_account_usdt(value):
    """Parse a positive, finite account value without exposing configuration data."""
    try:
        account_usdt = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("ACCOUNT_USDT must be a positive finite number") from error
    if not math.isfinite(account_usdt) or account_usdt <= 0:
        raise ValueError("ACCOUNT_USDT must be a positive finite number")
    return account_usdt


# ─── EXCHANGE DETECTION ───────────────────────────────────────────────────────
def detect_exchange():
    """Try each Binance endpoint; return the first that responds 200."""
    global BINANCE_BASE, MIN_24H_QUOTE_VOL, TOP_N_COINS
    for base, min_vol, max_coins in _EXCHANGE_OPTS:
        try:
            r = requests.get(
                f"{base}/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=8,
            )
            if r.status_code == 200:
                BINANCE_BASE = base
                MIN_24H_QUOTE_VOL = min_vol
                TOP_N_COINS = max_coins
                print(f"[INFO] Exchange: {base}  |  min_vol=${min_vol:,}  |  top_n={max_coins}")
                return
            print(f"[INFO] {base} → HTTP {r.status_code}, trying next...")
        except Exception as e:
            print(f"[INFO] {base} unreachable: {e}")
    # all failed — keep defaults (Binance.com) and let errors surface naturally
    print("[WARN] All exchange endpoints failed probe; proceeding with defaults")


# ─── SYMBOLS ──────────────────────────────────────────────────────────────────
def get_top_symbols():
    """Fetch top N USDT pairs sorted by 24h quote volume."""
    r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return select_top_symbols(r.json(), MIN_24H_QUOTE_VOL, TOP_N_COINS)


# ─── DATA ─────────────────────────────────────────────────────────────────────
def get_candles(symbol, interval):
    """Fetch closed candles from the selected Binance endpoint; drop the live candle."""
    params = {"symbol": symbol, "interval": interval, "limit": CANDLE_LIMIT}
    r = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 55:
        return None
    df = pd.DataFrame(
        rows,
        columns=[
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_ts",
            "quote_vol",
            "trades",
            "taker_base",
            "taker_quote",
            "ignore",
        ],
    )
    for col in ["open", "high", "low", "close", "volume", "quote_vol"]:
        df[col] = df[col].astype(float)
    return df.iloc[:-1].reset_index(drop=True)


# ─── INDICATORS ───────────────────────────────────────────────────────────────
def add_indicators(df):
    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_price = df["open"]
    volume = df["volume"]

    # Moving averages
    df["ma7"] = close.rolling(7).mean()
    df["ma14"] = close.rolling(14).mean()
    df["ma28"] = close.rolling(28).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()

    # ATR-14
    prev_c = close.shift(1)
    tr = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # Volume stats: compare each candle with the preceding 20 closed candles.
    df["vol_ma20"] = volume.rolling(20).mean().shift(1)
    df["rvol"] = volume / df["vol_ma20"].replace(0, float("nan"))

    # Candle structure
    df["candle_top"] = df[["open", "close"]].max(axis=1)
    df["candle_bot"] = df[["open", "close"]].min(axis=1)
    df["body"] = (close - open_price).abs()
    candle_range = (high - low).replace(0, float("nan"))
    df["body_pct"] = df["body"] / candle_range
    df["upper_wick"] = high - df["candle_top"]
    df["lower_wick"] = df["candle_bot"] - low
    df["is_bull"] = (close > open_price).astype(int)
    df["close_pos"] = (close - low) / candle_range  # 1.0 = high, 0.0 = low

    # 20-candle high/low before current candle
    df["high20"] = high.rolling(20).max().shift(1)
    df["low20"] = low.rolling(20).min().shift(1)

    return df.dropna().reset_index(drop=True)


# ─── ALERT TYPE ───────────────────────────────────────────────────────────────
def classify(row, direction, is_breakout, is_pullback):
    if direction == "bull":
        if is_breakout:
            return "Bullish Volume Breakout"
        if is_pullback:
            return "Bullish Pullback Rejection"
        if row["rvol"] >= 2.5:
            return "High-Volume Momentum Candle"
        return "Possible Reversal With Volume"

    if is_breakout:
        return "Bearish Volume Breakdown"
    if is_pullback:
        return "Bearish Pullback Rejection"
    if row["rvol"] >= 2.5:
        return "High-Volume Momentum Candle"
    return "Possible Reversal With Volume"


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram not configured")
        return False

    response = None
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as error:
        error_type = type(error).__name__
        status = getattr(response, "status_code", None)
        status_label = status if status is not None else "unavailable"
        print(f"[WARN] Telegram delivery failed: type={error_type} status={status_label}")
        return False
    return True


# ─── ANALYZE ONE SYMBOL/TF ────────────────────────────────────────────────────
def analyze(symbol, tf_label, interval, account_usdt):
    df_raw = get_candles(symbol, interval)
    if df_raw is None:
        return None, "no_data"

    df = add_indicators(df_raw)
    if len(df) < 5:
        return None, "no_data"

    row = df.iloc[-1]
    prev = df.iloc[-2]

    if row["rvol"] < VOLUME_SPIKE_MULT:
        return None, "low_vol"

    direction = "bull" if row["is_bull"] else "bear"

    score, reasons = calculate_score(row, df["volume"].iloc[-3:].tolist(), direction)
    if score < MIN_SCORE:
        return None, "low_score"

    risk = calculate_risk(
        row,
        df["low"].iloc[-6:-1].tolist(),
        df["high"].iloc[-6:-1].tolist(),
        direction,
        account_usdt,
        RISK_PCT,
        MAX_LEVERAGE,
    )
    if risk is None:
        return None, "bad_risk"

    is_breakout = (direction == "bull" and row["close"] > row["high20"]) or (
        direction == "bear" and row["close"] < row["low20"]
    )
    touched_ma = min(prev["low"], row["low"]) <= row["ma14"] <= max(prev["high"], row["high"])
    is_pullback = touched_ma and not is_breakout

    return {
        "symbol": symbol,
        "tf": tf_label,
        "direction": "LONG" if direction == "bull" else "SHORT",
        "alert_type": classify(row, direction, is_breakout, is_pullback),
        "score": score,
        "rvol": row["rvol"],
        "vol_spike_pct": (row["rvol"] - 1) * 100,
        "risk": risk,
        "breakout_lvl": row["high20"]
        if (is_breakout and direction == "bull")
        else row["low20"]
        if (is_breakout and direction == "bear")
        else None,
        "reasons": reasons,
    }, "ok"


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    try:
        account_usdt = parse_account_usdt(ACCOUNT_USDT)
    except ValueError as error:
        print(f"[ERR] Invalid configuration: {error}")
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"── Scan started {ts} ──")

    detect_exchange()

    try:
        symbols = get_top_symbols()
    except Exception as e:
        print(f"[ERR] Symbol fetch failed: {e}")
        return 0

    total = len(symbols) * len(TIMEFRAMES)
    print(f"Symbols: {len(symbols)}  ·  TFs: {len(TIMEFRAMES)}  ·  Total requests: {total}")

    stats = {"no_data": 0, "low_vol": 0, "low_score": 0, "bad_risk": 0, "ok": 0}
    candidates = []

    for symbol in symbols:
        for tf_label, interval in TIMEFRAMES.items():
            try:
                result, reason = analyze(symbol, tf_label, interval, account_usdt)
                stats[reason] = stats.get(reason, 0) + 1
                if result:
                    candidates.append(result)
            except Exception as e:
                print(f"[ERR] {symbol} {tf_label}: {e}")
                stats["no_data"] += 1
            time.sleep(API_SLEEP)

    # Per-run dedup: one alert per symbol (best timeframe wins)
    top = select_best_signals(candidates, MAX_ALERTS_PER_RUN)

    sent = 0
    for c in top:
        msg = format_alert(c, account_usdt, RISK_PCT, MAX_LEVERAGE)
        delivered = send_telegram(msg)
        delivery_label = "ALERT" if delivered else "ALERT FAILED"
        print(
            f"[{delivery_label}] {c['symbol']:12} {c['tf']:4} {c['direction']:5} "
            f"score={c['score']:3}  rvol={c['rvol']:.1f}x  {c['alert_type']}"
        )
        if delivered:
            sent += 1

    print(
        f"\n── Scan done ──\n"
        f"  Scanned:    {sum(stats.values())}\n"
        f"  Low volume: {stats.get('low_vol', 0)}\n"
        f"  Low score:  {stats.get('low_score', 0)}\n"
        f"  Bad risk:   {stats.get('bad_risk', 0)}\n"
        f"  Qualified:  {stats.get('ok', 0)}\n"
        f"  Alerts sent: {sent}"
    )
    if sent == 0:
        print("  No setups this scan.")

    # Daily heartbeat — fires once at HEARTBEAT_HOUR UTC (first 15-min window)
    now = datetime.now(timezone.utc)
    if now.hour == HEARTBEAT_HOUR and now.minute < 15:
        status = f"{sent} alert(s) sent this scan." if sent else "No setups this scan."
        heartbeat_delivered = send_telegram(
            f"🟢 *Scanner alive* — {ts}\n"
            f"Scanned `{len(symbols)}` pairs × `{len(TIMEFRAMES)}` TFs. {status}"
        )
        if heartbeat_delivered:
            print("[INFO] Daily heartbeat sent.")
        else:
            print("[WARN] Daily heartbeat failed.")

    return sent


if __name__ == "__main__":
    main()
