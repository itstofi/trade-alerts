# trade-alerts

Personal crypto scanner that pings me on Telegram when a setup I actually trade shows up. Runs on GitHub Actions — no server, no cost.

Watches BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, LTC across 15m / 1h / 4h. Silent when there's nothing. Noisy when there is.

---

## How it works

Three conditions all have to be true at the same time:

1. **Trend is clean** — price and MAs (7, 14, 28) are fully stacked in one direction
2. **Price pulled back** — recent candle wicked into the MA7 or MA14
3. **Rejection confirmed** — candle closed back in trend direction with the right wick structure

If all three hit, it sends a Telegram message with entry, stop, TP1, TP2, and position size based on 2% risk.

If not — nothing. No spam.

---

## Setup

You need a GitHub account and a Telegram account. Both free.

**Telegram bot**

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → grab the token
2. Message [@userinfobot](https://t.me/userinfobot) → grab your numeric chat ID
3. Start your new bot (send it any message so it can reach you)

**GitHub**

1. Fork or clone this repo
2. Go to Settings → Secrets and variables → Actions → add these three:

| Secret | Value |
|--------|-------|
| `TELEGRAM_TOKEN` | your BotFather token |
| `TELEGRAM_CHAT_ID` | your numeric chat ID |
| `ACCOUNT_USDT` | your account size (e.g. `500`) |

3. Actions tab → enable workflows → Run workflow to test

**Reliable scheduling (optional but recommended)**

GitHub's built-in cron can lag by 30–60 min. To fix that, use [cron-job.org](https://cron-job.org) to POST to:

```
https://api.github.com/repos/YOUR_USERNAME/trade-alerts/actions/workflows/trade-alerts.yml/dispatches
```

with a GitHub PAT and body `{"ref": "main"}` every 15 minutes.

---

## Customize

Edit the top of `scanner.py`:

```python
COINS = ["BTCUSDT", "ETHUSDT", ...]   # add or remove pairs
TIMEFRAMES = {"15m": "15m", "1h": "1h", "4h": "4h"}  # remove noisy ones
ACCOUNT_USDT = 500   # or set via secret
```

---

## What the alert looks like

```
🚨 TRADE QUALIFIED
BNBUSDT  •  4h  •  SHORT

🎯 PLAN
• Entry: 572.93
• Stop:  583.88  (−1.91%)
• TP1 (1.5R): 556.50  — close 50%, stop→BE
• TP2 (3R):   540.07  — trail rest

💰 SIZE
• Account: 500 USDT
• Max loss: 10.00 USDT (2%)
• Notional: 523.04 USDT
• Leverage: 1.05x (≤3x)
```

---

Data from Binance US public API. No API key needed.

Not financial advice — confirm on the chart before entering.
