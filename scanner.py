#!/usr/bin/env python3
"""
Trade Alert Scanner
Watches a list of coins across multiple timeframes on Bybit and sends a
Telegram alert ONLY when a setup qualifies under the trading-chart-analyst rules.

Rules enforced (mirrors the skill):
- Trend filter: price + MA7/MA14/MA28 must be cleanly stacked (up=LONG, down=SHORT)
- Entry trigger: pullback to MA7/MA14 + rejection wick + close back in trend direction
- Risk: 2% account, max 3x leverage, min 1.5:1 reward:risk
- No qualifying setup => silent (no spam). Only QUALIFIED setups alert.

Config via environment variables (set as GitHub Secrets):
  TELEGRAM_TOKEN   - your bot token from @BotFather
  TELEGRAM_CHAT_ID - your chat id
  ACCOUNT_USDT     - account size, default 100
"""

import os
import time
import requests
import pandas as pd

# ---------------- CONFIG ----------------
COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT",
]

# Bybit interval codes: 15 = 15m, 60 = 1h, 240 = 4h
TIMEFRAMES = {"15m": "15", "1h": "60", "4h": "240"}

ACCOUNT_USDT = float(os.getenv("ACCOUNT_USDT", "100"))
RISK_PCT = 0.02          # 2% max risk
MAX_LEVERAGE = 3.0       # hard cap
MIN_RR = 1.5             # minimum reward:risk

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"


# ---------------- DATA ----------------
def get_candles(symbol, interval, limit=120):
    """Fetch candles from Bybit public API. No key needed."""
    params = {"category": "linear", "symbol": symbol,
              "interval": interval, "limit": limit}
    r = requests.get(BYBIT_KLINE, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"{symbol} {interval}: {data.get('retMsg')}")
    rows = data["result"]["list"]  # newest first
    df = pd.DataFrame(rows, columns=[
        "ts", "open", "high", "low", "close", "volume", "turnover"])
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    df = df.iloc[::-1].reset_index(drop=True)  # oldest first
    return df


def add_mas(df):
    df["ma7"] = df["close"].rolling(7).mean()
    df["ma14"] = df["close"].rolling(14).mean()
    df["ma28"] = df["close"].rolling(28).mean()
    return df


# ---------------- RULE ENGINE ----------------
def analyze(symbol, tf_label, df):
    """Return a trade plan dict if QUALIFIED, else None."""
    df = add_mas(df).dropna().reset_index(drop=True)
    if len(df) < 3:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = last["close"]

    # --- Step 2: Trend filter ---
    stacked_up = price > last["ma7"] > last["ma14"] > last["ma28"]
    stacked_dn = price < last["ma7"] < last["ma14"] < last["ma28"]
    if not (stacked_up or stacked_dn):
        return None  # chop -> no trade
    direction = "LONG" if stacked_up else "SHORT"

    # --- Step 3: Entry trigger (pullback to MA7/MA14 + rejection) ---
    # Did the recent candle wick into MA7 or MA14 and close back in trend dir?
    touched = (min(prev["low"], last["low"]) <= last["ma7"] <= max(prev["high"], last["high"])) or \
              (min(prev["low"], last["low"]) <= last["ma14"] <= max(prev["high"], last["high"]))
    if not touched:
        return None

    if direction == "LONG":
        # rejection: lower wick + green close, closing above MA7
        candle_body_ok = last["close"] > last["open"]
        lower_wick = (min(last["open"], last["close"]) - last["low"])
        upper_wick = (last["high"] - max(last["open"], last["close"]))
        rejection = lower_wick > upper_wick and last["close"] > last["ma7"]
        if not (candle_body_ok and rejection):
            return None
        # swing low for stop = lowest low of last 5 candles
        swing = df["low"].iloc[-6:-1].min()
        stop = min(swing, last["low"]) * 0.999
        risk_per_unit = price - stop
        tp1 = price + risk_per_unit * 1.5
        tp2 = price + risk_per_unit * 3.0
    else:  # SHORT
        candle_body_ok = last["close"] < last["open"]
        upper_wick = (last["high"] - max(last["open"], last["close"]))
        lower_wick = (min(last["open"], last["close"]) - last["low"])
        rejection = upper_wick > lower_wick and last["close"] < last["ma7"]
        if not (candle_body_ok and rejection):
            return None
        swing = df["high"].iloc[-6:-1].max()
        stop = max(swing, last["high"]) * 1.001
        risk_per_unit = stop - price
        tp1 = price - risk_per_unit * 1.5
        tp2 = price - risk_per_unit * 3.0

    if risk_per_unit <= 0:
        return None

    # --- Step 4: Reward:Risk check ---
    rr = 1.5  # by construction TP1 is 1.5R; gate stays for safety
    if rr < MIN_RR:
        return None

    # --- Step 5: Position size & leverage (derived) ---
    risk_amount = ACCOUNT_USDT * RISK_PCT
    stop_dist_pct = abs(price - stop) / price
    if stop_dist_pct == 0:
        return None
    notional = risk_amount / stop_dist_pct
    leverage = notional / ACCOUNT_USDT
    if leverage > MAX_LEVERAGE:
        leverage = MAX_LEVERAGE
        notional = ACCOUNT_USDT * MAX_LEVERAGE  # cap notional, smaller position

    return {
        "symbol": symbol, "tf": tf_label, "direction": direction,
        "entry": price, "stop": stop, "tp1": tp1, "tp2": tp2,
        "stop_pct": stop_dist_pct * 100, "risk_amount": risk_amount,
        "notional": notional, "leverage": leverage,
    }


# ---------------- ALERT ----------------
def fmt(p):
    return f"{p:.6f}".rstrip("0").rstrip(".") if p < 1 else f"{p:,.4f}".rstrip("0").rstrip(".")


def build_message(plan):
    d = plan
    return (
        f"\U0001F6A8 *TRADE QUALIFIED*\n"
        f"`{d['symbol']}`  \u2022  *{d['tf']}*  \u2022  *{d['direction']}*\n\n"
        f"\U0001F3AF *PLAN*\n"
        f"\u2022 Entry: `{fmt(d['entry'])}`\n"
        f"\u2022 Stop:  `{fmt(d['stop'])}`  (\u2212{d['stop_pct']:.2f}%)\n"
        f"\u2022 TP1 (1.5R): `{fmt(d['tp1'])}`  \u2014 close 50%, stop\u2192BE\n"
        f"\u2022 TP2 (3R):   `{fmt(d['tp2'])}`  \u2014 trail rest\n"
        f"\u2022 R:R: 1.5:1 (TP1) / 3:1 (TP2)\n\n"
        f"\U0001F4B0 *SIZE*\n"
        f"\u2022 Account: {ACCOUNT_USDT:.0f} USDT\n"
        f"\u2022 Max loss: {d['risk_amount']:.2f} USDT (2%)\n"
        f"\u2022 Notional: {d['notional']:.2f} USDT\n"
        f"\u2022 Leverage: {d['leverage']:.2f}x (\u22643x)\n\n"
        f"\u26A0\uFE0F Not financial advice. One TF only \u2014 no order book/funding/macro. "
        f"You own the trade."
    )


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram not configured, printing instead:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown",
    }, timeout=15)


# ---------------- MAIN ----------------
def main():
    qualified = 0
    for symbol in COINS:
        for tf_label, interval in TIMEFRAMES.items():
            try:
                df = get_candles(symbol, interval)
                plan = analyze(symbol, tf_label, df)
                if plan:
                    qualified += 1
                    msg = build_message(plan)
                    send_telegram(msg)
                    print(f"[ALERT] {symbol} {tf_label} {plan['direction']}")
                else:
                    print(f"[ok]    {symbol} {tf_label} no setup")
                time.sleep(0.3)  # be gentle with the API
            except Exception as e:
                print(f"[ERR]   {symbol} {tf_label}: {e}")
    print(f"Scan done. {qualified} qualified setup(s).")


if __name__ == "__main__":
    main()
