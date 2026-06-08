#!/usr/bin/env python3
# Market mover scanner — top 200 Binance USDT pairs
# Alerts only on real movement: volume spike + strong candle + scoring system
# Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_USDT

import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TIMEFRAMES          = {"15m": "15m", "1h": "1h", "4h": "4h"}
MIN_SCORE           = 80           # alert threshold 0-100
VOLUME_SPIKE_MULT   = 2.0          # min RVOL vs 20-candle avg (early filter)
MAX_ALERTS_PER_RUN  = 5            # send only top N by score per scan
HEARTBEAT_HOUR      = 8            # UTC hour for daily alive ping (0–23)
CANDLE_LIMIT        = 100          # candles fetched per request
ACCOUNT_USDT        = float(os.getenv("ACCOUNT_USDT", "100"))
RISK_PCT            = 0.02
MAX_LEVERAGE        = 3.0
MIN_RR              = 1.5
API_SLEEP           = 0.25         # seconds between API calls
REQUEST_TIMEOUT     = 15

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Updated at startup by detect_exchange() — do not set these manually
BINANCE_BASE       = "https://api.binance.com"
MIN_24H_QUOTE_VOL  = 5_000_000
TOP_N_COINS        = 200

STABLECOINS = {"USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "USDD", "FRAX"}
SKIP_TOKENS = {"UP", "DOWN", "BULL", "BEAR"}

# Ordered list of (base_url, min_vol, max_coins) — first accessible one wins
_EXCHANGE_OPTS = [
    ("https://api.binance.com", 5_000_000, 200),  # global Binance — 600+ pairs
    ("https://api.binance.us",  25_000,    50),   # US fallback — ~15 active pairs
]


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
                BINANCE_BASE      = base
                MIN_24H_QUOTE_VOL = min_vol
                TOP_N_COINS       = max_coins
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
    result = []
    for t in r.json():
        sym  = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLECOINS:
            continue
        if any(tok in base for tok in SKIP_TOKENS):
            continue
        qvol = float(t.get("quoteVolume", 0))
        if qvol < MIN_24H_QUOTE_VOL:
            continue
        result.append((sym, qvol))
    result.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in result[:TOP_N_COINS]]


# ─── DATA ─────────────────────────────────────────────────────────────────────
def get_candles(symbol, interval):
    """Fetch closed candles from Binance US. Drops the live unfinished candle."""
    params = {"symbol": symbol, "interval": interval, "limit": CANDLE_LIMIT}
    r = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 55:
        return None
    df = pd.DataFrame(rows, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "close_ts", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume", "quote_vol"]:
        df[col] = df[col].astype(float)
    return df.iloc[:-1].reset_index(drop=True)


# ─── INDICATORS ───────────────────────────────────────────────────────────────
def add_indicators(df):
    c, h, l, o, v = df["close"], df["high"], df["low"], df["open"], df["volume"]

    # Moving averages
    df["ma7"]   = c.rolling(7).mean()
    df["ma14"]  = c.rolling(14).mean()
    df["ma28"]  = c.rolling(28).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()

    # ATR-14
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # Volume stats
    df["vol_ma20"] = v.rolling(20).mean()
    df["rvol"]     = v / df["vol_ma20"].replace(0, float("nan"))

    # Candle structure
    df["candle_top"] = df[["open", "close"]].max(axis=1)
    df["candle_bot"] = df[["open", "close"]].min(axis=1)
    df["body"]       = (c - o).abs()
    rng              = (h - l).replace(0, float("nan"))
    df["body_pct"]   = df["body"] / rng
    df["upper_wick"] = h - df["candle_top"]
    df["lower_wick"] = df["candle_bot"] - l
    df["is_bull"]    = (c > o).astype(int)
    df["close_pos"]  = (c - l) / rng  # 1.0 = closed at high, 0.0 = at low

    # 20-candle high/low before current candle
    df["high20"] = h.rolling(20).max().shift(1)
    df["low20"]  = l.rolling(20).min().shift(1)

    return df.dropna().reset_index(drop=True)


# ─── SCORING (0–100) ──────────────────────────────────────────────────────────
def calc_score(row, df, direction):
    pts     = 0
    reasons = []

    # 1. Volume spike  (max 30 pts)
    rvol  = row["rvol"]
    v_pts = 0
    if   rvol >= 4.0: v_pts = 30
    elif rvol >= 3.0: v_pts = 25
    elif rvol >= 2.0: v_pts = 20
    elif rvol >= 1.5: v_pts = 12
    elif rvol >= 1.2: v_pts = 5

    last3 = df["volume"].iloc[-3:].values
    if last3[0] < last3[1] < last3[2]:
        v_pts = min(30, v_pts + 5)
        reasons.append("rising vol x3")

    if v_pts >= 12:
        reasons.append(f"RVOL {rvol:.1f}x")
    pts += v_pts

    # 2. Candle strength  (max 25 pts)
    bp    = row["body_pct"]
    pa    = 0
    if   bp >= 0.70: pa = 25
    elif bp >= 0.55: pa = 20
    elif bp >= 0.40: pa = 14
    elif bp >= 0.25: pa = 7

    cp = row["close_pos"]
    if direction == "bull" and cp >= 0.80:
        pa = min(25, pa + 5)
        reasons.append("close near high")
    elif direction == "bear" and cp <= 0.20:
        pa = min(25, pa + 5)
        reasons.append("close near low")
    pts += pa

    # 3. Breakout / breakdown  (max 20 pts)
    price = row["close"]
    bo    = 0
    if direction == "bull" and price > row["high20"]:
        bo = 20
        reasons.append(f"broke above {fmt(row['high20'])}")
    elif direction == "bear" and price < row["low20"]:
        bo = 20
        reasons.append(f"broke below {fmt(row['low20'])}")
    elif direction == "bull" and price > row["ma28"] and price > row["ema50"]:
        bo = 10
    elif direction == "bear" and price < row["ma28"] and price < row["ema50"]:
        bo = 10
    pts += bo

    # 4. Trend alignment  (max 15 pts)
    tr = 0
    if direction == "bull":
        if price > row["ma7"] > row["ma14"] > row["ma28"]:
            tr = 15
            reasons.append("MAs stacked bull")
        elif price > row["ma14"] > row["ma28"]: tr = 10
        elif price > row["ma28"]:               tr = 5
    else:
        if price < row["ma7"] < row["ma14"] < row["ma28"]:
            tr = 15
            reasons.append("MAs stacked bear")
        elif price < row["ma14"] < row["ma28"]: tr = 10
        elif price < row["ma28"]:               tr = 5
    pts += tr

    # 5. ATR expansion  (max 10 pts)
    at = 0
    if row["atr"] > 0:
        ratio = row["body"] / row["atr"]
        if   ratio >= 2.0: at = 10; reasons.append(f"body {ratio:.1f}x ATR")
        elif ratio >= 1.5: at = 7
        elif ratio >= 1.0: at = 4
    pts += at

    return min(100, pts), reasons


# ─── ALERT TYPE ───────────────────────────────────────────────────────────────
def classify(row, direction, is_breakout, is_pullback):
    if direction == "bull":
        if is_breakout: return "Bullish Volume Breakout"
        if is_pullback: return "Bullish Pullback Rejection"
        if row["rvol"] >= 2.5: return "High-Volume Momentum Candle"
        return "Possible Reversal With Volume"
    else:
        if is_breakout: return "Bearish Volume Breakdown"
        if is_pullback: return "Bearish Pullback Rejection"
        if row["rvol"] >= 2.5: return "High-Volume Momentum Candle"
        return "Possible Reversal With Volume"


# ─── RISK ─────────────────────────────────────────────────────────────────────
def calc_risk(row, df, direction):
    price = row["close"]
    atr   = row["atr"]

    if direction == "bull":
        swing = df["low"].iloc[-6:-1].min()
        stop  = min(swing, row["low"]) - atr * 0.3
        risk_unit = price - stop
        tp1 = price + risk_unit * 1.5
        tp2 = price + risk_unit * 3.0
    else:
        swing = df["high"].iloc[-6:-1].max()
        stop  = max(swing, row["high"]) + atr * 0.3
        risk_unit = stop - price
        tp1 = price - risk_unit * 1.5
        tp2 = price - risk_unit * 3.0

    if risk_unit <= 0:
        return None
    stop_pct = risk_unit / price
    if stop_pct < 0.003 or stop_pct > 0.12:
        return None

    risk_amt = ACCOUNT_USDT * RISK_PCT
    notional = risk_amt / stop_pct
    leverage = min(notional / ACCOUNT_USDT, MAX_LEVERAGE)
    notional = ACCOUNT_USDT * leverage if leverage == MAX_LEVERAGE else notional

    return {
        "entry": price, "stop": stop, "tp1": tp1, "tp2": tp2,
        "stop_pct": stop_pct * 100, "risk_amt": risk_amt,
        "notional": notional, "leverage": leverage,
    }


# ─── FORMAT & MESSAGE ─────────────────────────────────────────────────────────
def fmt(p):
    if p < 0.001: return f"{p:.8f}".rstrip("0")
    if p < 1:     return f"{p:.6f}".rstrip("0")
    if p < 100:   return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:,.2f}"


def build_message(r):
    d          = r["risk"]
    tag        = "🟢" if r["direction"] == "LONG" else "🔴"
    why        = " · ".join(r["reasons"]) if r["reasons"] else "volume + momentum"
    level_line = f"· Level: `{fmt(r['breakout_lvl'])}`\n" if r["breakout_lvl"] else ""

    return (
        f"🚨 *{r['alert_type'].upper()}*\n"
        f"`{r['symbol']}`  ·  *{r['tf']}*  ·  {tag} *{r['direction']}*  ·  Score: *{r['score']}/100*\n\n"
        f"📊 *SIGNAL*\n"
        f"· Price: `{fmt(d['entry'])}`\n"
        f"· Vol spike: `+{r['vol_spike_pct']:.0f}%` above 20-candle avg\n"
        f"· RVOL: `{r['rvol']:.1f}x`\n"
        f"{level_line}"
        f"· Why: _{why}_\n\n"
        f"🎯 *PLAN*\n"
        f"· Entry:       `{fmt(d['entry'])}`\n"
        f"· Stop:        `{fmt(d['stop'])}`  (−{d['stop_pct']:.2f}%)\n"
        f"· TP1 (1.5R):  `{fmt(d['tp1'])}`  — close 50%, stop→BE\n"
        f"· TP2 (3R):    `{fmt(d['tp2'])}`  — trail rest\n"
        f"· R:R:         1.5:1 / 3:1\n\n"
        f"💰 *SIZE*\n"
        f"· Account: {ACCOUNT_USDT:.0f} USDT\n"
        f"· Max loss: {d['risk_amt']:.2f} USDT (2%)\n"
        f"· Notional: {d['notional']:.2f} USDT\n"
        f"· Leverage: {d['leverage']:.2f}x (≤3x)\n\n"
        f"⚠️ Not financial advice. Confirm on chart before entering."
    )


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram not configured:\n", text[:300])
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"[WARN] Telegram error: {e}")


# ─── ANALYZE ONE SYMBOL/TF ────────────────────────────────────────────────────
def analyze(symbol, tf_label, interval):
    df_raw = get_candles(symbol, interval)
    if df_raw is None:
        return None, "no_data"

    df = add_indicators(df_raw)
    if len(df) < 5:
        return None, "no_data"

    row  = df.iloc[-1]
    prev = df.iloc[-2]

    if row["rvol"] < VOLUME_SPIKE_MULT:
        return None, "low_vol"

    direction = "bull" if row["is_bull"] else "bear"

    score, reasons = calc_score(row, df, direction)
    if score < MIN_SCORE:
        return None, "low_score"

    risk = calc_risk(row, df, direction)
    if risk is None:
        return None, "bad_risk"

    is_breakout = (
        (direction == "bull" and row["close"] > row["high20"]) or
        (direction == "bear" and row["close"] < row["low20"])
    )
    touched_ma  = min(prev["low"], row["low"]) <= row["ma14"] <= max(prev["high"], row["high"])
    is_pullback = touched_ma and not is_breakout

    return {
        "symbol":        symbol,
        "tf":            tf_label,
        "direction":     "LONG" if direction == "bull" else "SHORT",
        "alert_type":    classify(row, direction, is_breakout, is_pullback),
        "score":         score,
        "rvol":          row["rvol"],
        "vol_spike_pct": (row["rvol"] - 1) * 100,
        "risk":          risk,
        "breakout_lvl":  row["high20"] if (is_breakout and direction == "bull")
                         else row["low20"] if (is_breakout and direction == "bear")
                         else None,
        "reasons":       reasons,
    }, "ok"


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"── Scan started {ts} ──")

    detect_exchange()

    try:
        symbols = get_top_symbols()
    except Exception as e:
        print(f"[ERR] Symbol fetch failed: {e}")
        return

    total = len(symbols) * len(TIMEFRAMES)
    print(f"Symbols: {len(symbols)}  ·  TFs: {len(TIMEFRAMES)}  ·  Total requests: {total}")

    stats      = {"no_data": 0, "low_vol": 0, "low_score": 0, "bad_risk": 0, "ok": 0}
    candidates = []

    for symbol in symbols:
        for tf_label, interval in TIMEFRAMES.items():
            try:
                result, reason = analyze(symbol, tf_label, interval)
                stats[reason]  = stats.get(reason, 0) + 1
                if result:
                    candidates.append(result)
            except Exception as e:
                print(f"[ERR] {symbol} {tf_label}: {e}")
                stats["no_data"] += 1
            time.sleep(API_SLEEP)

    # Per-run dedup: one alert per symbol (best timeframe wins)
    best = {}
    for c in candidates:
        sym = c["symbol"]
        if sym not in best or c["score"] > best[sym]["score"]:
            best[sym] = c
    top = sorted(best.values(), key=lambda x: x["score"], reverse=True)

    sent = 0
    for c in top[:MAX_ALERTS_PER_RUN]:
        msg = build_message(c)
        send_telegram(msg)
        print(
            f"[ALERT] {c['symbol']:12} {c['tf']:4} {c['direction']:5} "
            f"score={c['score']:3}  rvol={c['rvol']:.1f}x  {c['alert_type']}"
        )
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
        send_telegram(
            f"🟢 *Scanner alive* — {ts}\n"
            f"Scanned `{len(symbols)}` pairs × `{len(TIMEFRAMES)}` TFs. {status}"
        )
        print("[INFO] Daily heartbeat sent.")


if __name__ == "__main__":
    main()
