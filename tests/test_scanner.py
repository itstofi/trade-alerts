import os
import subprocess
import sys
from datetime import datetime

import pandas as pd
import pytest
import requests

import scanner


def test_rvol_baseline_excludes_current_candle():
    rows = 60
    frame = pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [102.0] * rows,
            "low": [99.0] * rows,
            "close": [101.0] * rows,
            "volume": [100.0] * (rows - 1) + [200.0],
        }
    )

    result = scanner.add_indicators(frame)

    assert result.iloc[-1]["rvol"] == pytest.approx(2.0)


@pytest.mark.parametrize("value", ["oops", "0", "-1", "NaN", "Infinity", "-Infinity"])
def test_parse_account_usdt_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="ACCOUNT_USDT must be a positive finite number"):
        scanner.parse_account_usdt(value)


def test_malformed_account_environment_does_not_crash_import():
    environment = os.environ.copy()
    environment["ACCOUNT_USDT"] = "not-a-number"

    result = subprocess.run(
        [sys.executable, "-c", "import scanner"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr


def test_main_rejects_invalid_account_before_scanning(monkeypatch, capsys):
    monkeypatch.setattr(scanner, "ACCOUNT_USDT", "not-a-number")
    monkeypatch.setattr(
        scanner,
        "detect_exchange",
        lambda: pytest.fail("exchange probe must not run with invalid configuration"),
    )

    assert scanner.main() == 0
    assert (
        "[ERR] Invalid configuration: ACCOUNT_USDT must be a positive finite number"
        in capsys.readouterr().out
    )


def test_send_telegram_returns_false_without_credentials(monkeypatch, capsys):
    monkeypatch.setattr(scanner, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(scanner, "TELEGRAM_CHAT_ID", "")

    assert scanner.send_telegram("private alert body") is False
    output = capsys.readouterr().out
    assert "Telegram not configured" in output
    assert "private alert body" not in output


def test_send_telegram_raises_for_non_2xx_and_logs_only_type_and_status(monkeypatch, capsys):
    class Response:
        status_code = 502

        def raise_for_status(self):
            raise requests.HTTPError("https://api.telegram.org/botSECRET/sendMessage failed")

    monkeypatch.setattr(scanner, "TELEGRAM_TOKEN", "SECRET")
    monkeypatch.setattr(scanner, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: Response())

    assert scanner.send_telegram("private alert body") is False
    output = capsys.readouterr().out
    assert "HTTPError" in output
    assert "status=502" in output
    assert "SECRET" not in output
    assert "https://" not in output
    assert "private alert body" not in output


def test_send_telegram_sanitizes_unexpected_exception(monkeypatch, capsys):
    def fail_post(*args, **kwargs):
        raise RuntimeError("https://api.telegram.org/botSECRET/sendMessage failed")

    monkeypatch.setattr(scanner, "TELEGRAM_TOKEN", "SECRET")
    monkeypatch.setattr(scanner, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(scanner.requests, "post", fail_post)

    assert scanner.send_telegram("private alert body") is False
    output = capsys.readouterr().out
    assert "type=RuntimeError status=unavailable" in output
    assert "SECRET" not in output
    assert "https://" not in output
    assert "private alert body" not in output


def test_send_telegram_returns_true_after_status_check(monkeypatch):
    class Response:
        status_code = 200
        status_checked = False

        def raise_for_status(self):
            self.status_checked = True

    response = Response()
    monkeypatch.setattr(scanner, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(scanner, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(scanner.requests, "post", lambda *args, **kwargs: response)

    assert scanner.send_telegram("alert") is True
    assert response.status_checked is True


@pytest.mark.parametrize(("delivery_succeeded", "expected_sent"), [(False, 0), (True, 1)])
def test_main_counts_only_successful_telegram_deliveries(
    monkeypatch, capsys, delivery_succeeded, expected_sent
):
    class FixedDateTime:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 1, 1, 9, 30, tzinfo=tz)

    candidate = {
        "symbol": "BTCUSDT",
        "tf": "1h",
        "direction": "LONG",
        "score": 90,
        "rvol": 2.5,
        "alert_type": "Bullish Volume Breakout",
    }
    monkeypatch.setattr(scanner, "ACCOUNT_USDT", "100")
    monkeypatch.setattr(scanner, "TIMEFRAMES", {"1h": "1h"})
    monkeypatch.setattr(scanner, "datetime", FixedDateTime)
    monkeypatch.setattr(scanner, "detect_exchange", lambda: None)
    monkeypatch.setattr(scanner, "get_top_symbols", lambda: ["BTCUSDT"])
    monkeypatch.setattr(scanner, "analyze", lambda *args: (candidate, "ok"))
    monkeypatch.setattr(scanner, "format_alert", lambda *args: "alert")
    monkeypatch.setattr(scanner, "send_telegram", lambda text: delivery_succeeded)
    monkeypatch.setattr(scanner.time, "sleep", lambda seconds: None)

    assert scanner.main() == expected_sent
    assert f"Alerts sent: {expected_sent}" in capsys.readouterr().out


def test_main_reports_failed_heartbeat_delivery(monkeypatch, capsys):
    class FixedDateTime:
        @classmethod
        def now(cls, tz):
            return datetime(2026, 1, 1, 8, 5, tzinfo=tz)

    monkeypatch.setattr(scanner, "ACCOUNT_USDT", "100")
    monkeypatch.setattr(scanner, "TIMEFRAMES", {"1h": "1h"})
    monkeypatch.setattr(scanner, "datetime", FixedDateTime)
    monkeypatch.setattr(scanner, "detect_exchange", lambda: None)
    monkeypatch.setattr(scanner, "get_top_symbols", lambda: [])
    monkeypatch.setattr(scanner, "send_telegram", lambda text: False)

    assert scanner.main() == 0
    output = capsys.readouterr().out
    assert "Daily heartbeat failed" in output
    assert "Daily heartbeat sent" not in output
