from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ibkr_strategy_runner.leaps_strategy import LeapsStrategyConfig, LeapsTrader


class FakeHistoricalClient:
    def __init__(self, closes: list[float]) -> None:
        self.closes = closes

    def historical_daily_bars(
        self,
        symbol: str,
        duration: str,
        primary_exchange: str | None = None,
    ) -> list[dict[str, object]]:
        return [
            {"date": f"2026-04-{index + 1:02d}", "close": close}
            for index, close in enumerate(self.closes[:-1])
        ] + [{"date": "2026-05-01", "close": self.closes[-1]}]


class ExplainStrategyTest(unittest.TestCase):
    def test_explain_today_holds_when_signal_not_triggered(self) -> None:
        trader = LeapsTrader(
            FakeHistoricalClient([100.0, 101.0, 102.0]),
            LeapsStrategyConfig(signal_drop=-0.01),
            state_store=object(),
        )

        explanation = trader.explain_today()

        self.assertEqual(explanation["decision"]["option_entry"], "hold")
        self.assertIn("is above signal_drop", explanation["decision"]["reason"])
        self.assertFalse(explanation["signal"]["triggered"])

    def test_explain_today_enters_when_signal_triggered(self) -> None:
        trader = LeapsTrader(
            FakeHistoricalClient([100.0, 101.0, 98.0]),
            LeapsStrategyConfig(signal_drop=-0.01),
            state_store=object(),
        )

        explanation = trader.explain_today()

        self.assertEqual(explanation["decision"]["option_entry"], "would_enter")
        self.assertIn("is at or below signal_drop", explanation["decision"]["reason"])
        self.assertTrue(explanation["signal"]["triggered"])

    def test_strategy_config_round_trip_uses_live_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "symbol": "QQQ",
                        "signal_drop": -0.02,
                        "target_delta": 0.55,
                        "stale_order_policy": "cancel_before_cycle",
                        "take_profit_rules": [
                            {"max_holding_days": 90, "pct_gain": 0.25},
                        ],
                    }
                )
            )

            config = LeapsStrategyConfig.from_file(path)
            payload = config.to_json_dict()

            self.assertEqual(payload["symbol"], "QQQ")
            self.assertEqual(payload["signal_drop"], -0.02)
            self.assertEqual(payload["target_delta"], 0.55)
            self.assertEqual(payload["stale_order_policy"], "cancel_before_cycle")
            self.assertEqual(payload["take_profit_rules"][0]["pct_gain"], 0.25)


if __name__ == "__main__":
    unittest.main()
