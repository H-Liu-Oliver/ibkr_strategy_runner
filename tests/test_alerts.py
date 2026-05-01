from __future__ import annotations

import argparse
import io
import json
import unittest

from ibkr_strategy_runner.alerts import AlertEvent, AlertSink, alert_events_from_cycle
from ibkr_strategy_runner.cli import cmd_alert_test
from ibkr_strategy_runner.config import Settings
from ibkr_strategy_runner.leaps_strategy import CycleResult


def make_settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=4002,
        client_id=201,
        account="DU123456",
        allow_order=False,
        allow_live_trading=False,
        live_account_allowlist=(),
        default_exchange="SMART",
        default_currency="USD",
        connect_timeout=10.0,
        request_timeout=15.0,
        market_data_type=3,
    )


class AlertTest(unittest.TestCase):
    def test_generates_alerts_from_cycle_actions(self) -> None:
        result = CycleResult(date="2026-05-01", mode="execute", account="DU123456", symbol="QQQ")
        result.actions.extend(
            [
                {
                    "type": "reconcile",
                    "action": "CHECK_ORDER_STATUS",
                    "reason": "unknown order status",
                    "blocking": True,
                },
                {
                    "type": "dca",
                    "action": "HOLD",
                    "reason": "risk limit max_daily_notional exceeded",
                },
                {
                    "type": "dca",
                    "action": "BUY",
                    "symbol": "QQQ",
                    "execute": True,
                    "order": {"status": "Submitted"},
                },
                {
                    "type": "reconcile",
                    "action": "PENDING_ORDER_CLEARED",
                    "fills": [{"execId": "fill-1"}],
                },
            ]
        )

        events = alert_events_from_cycle(result)

        self.assertEqual(
            [event.event_type for event in events],
            ["reconciliation_mismatch", "risk_limit_breach", "order_submitted", "fill"],
        )

    def test_alert_sink_logs_and_skips_webhook_in_dry_run(self) -> None:
        stream = io.StringIO()
        sink = AlertSink(webhook_url="https://example.invalid/webhook", dry_run=True, stream=stream)

        result = sink.emit(AlertEvent("test", "info", "hello"))

        self.assertFalse(result["webhookDelivered"])
        self.assertTrue(result["dryRun"])
        logged = json.loads(stream.getvalue())["alert"]
        self.assertEqual(logged["event_type"], "test")

    def test_alert_test_command_supports_webhook_dry_run(self) -> None:
        result = cmd_alert_test(
            make_settings(),
            argparse.Namespace(
                webhook_url="https://example.invalid/webhook",
                dry_run=True,
                message="test message",
            ),
        )

        self.assertEqual(result["event"]["message"], "test message")
        self.assertEqual(result["webhookUrl"], "https://example.invalid/webhook")
        self.assertFalse(result["webhookDelivered"])


if __name__ == "__main__":
    unittest.main()
