from __future__ import annotations

import unittest

from ibkr_strategy_runner.leaps_strategy import (
    LeapsStrategyConfig,
    LeapsTrader,
    bid_ask_spread_pct,
    daily_order_usage,
    total_open_order_value,
    underlying_position_value,
)
from ibkr_strategy_runner.live_state import StrategyState


def make_trader(config: LeapsStrategyConfig) -> LeapsTrader:
    return LeapsTrader(client=object(), config=config, state_store=object())


class RiskLimitTest(unittest.TestCase):
    def test_blocks_single_order_value(self) -> None:
        trader = make_trader(LeapsStrategyConfig(max_single_order_value=1000))

        reason = trader._risk_block_reason(
            StrategyState(),
            "2026-05-01",
            "stock",
            order_value=1000.01,
        )

        self.assertIn("max_single_order_value", reason or "")

    def test_blocks_daily_order_count(self) -> None:
        state = StrategyState(
            pending_orders=[
                {
                    "createdDate": "2026-05-01",
                    "orderValue": 100.0,
                }
            ]
        )
        trader = make_trader(LeapsStrategyConfig(max_daily_order_count=1))

        reason = trader._risk_block_reason(
            state,
            "2026-05-01",
            "stock",
            order_value=100.0,
        )

        self.assertIn("max_daily_order_count", reason or "")

    def test_blocks_daily_notional(self) -> None:
        state = StrategyState(
            pending_orders=[
                {
                    "createdDate": "2026-05-01",
                    "orderValue": 750.0,
                }
            ]
        )
        trader = make_trader(LeapsStrategyConfig(max_daily_notional=1000.0))

        reason = trader._risk_block_reason(
            state,
            "2026-05-01",
            "stock",
            order_value=251.0,
        )

        self.assertIn("max_daily_notional", reason or "")

    def test_blocks_total_open_order_value(self) -> None:
        state = StrategyState(
            pending_orders=[
                {
                    "orderValue": 900.0,
                }
            ]
        )
        trader = make_trader(LeapsStrategyConfig(max_total_open_order_value=1000.0))

        reason = trader._risk_block_reason(
            state,
            "2026-05-01",
            "stock",
            order_value=101.0,
        )

        self.assertIn("max_total_open_order_value", reason or "")

    def test_blocks_stock_position_value(self) -> None:
        trader = make_trader(LeapsStrategyConfig(max_stock_position_value=1000.0))

        reason = trader._risk_block_reason(
            StrategyState(),
            "2026-05-01",
            "stock",
            order_value=101.0,
            current_stock_value=900.0,
        )

        self.assertIn("max_stock_position_value", reason or "")

    def test_blocks_option_position_value(self) -> None:
        trader = make_trader(LeapsStrategyConfig(max_option_position_value=1000.0))

        reason = trader._risk_block_reason(
            StrategyState(),
            "2026-05-01",
            "option",
            order_value=101.0,
            option_value=900.0,
            quote={"bid": 1.0, "ask": 1.1},
        )

        self.assertIn("max_option_position_value", reason or "")

    def test_blocks_wide_option_spread(self) -> None:
        trader = make_trader(LeapsStrategyConfig(max_option_bid_ask_spread_pct=0.10))

        reason = trader._risk_block_reason(
            StrategyState(),
            "2026-05-01",
            "option",
            order_value=100.0,
            quote={"bid": 1.0, "ask": 1.3},
        )

        self.assertIn("max_option_bid_ask_spread_pct", reason or "")

    def test_allows_order_within_limits(self) -> None:
        trader = make_trader(
            LeapsStrategyConfig(
                max_single_order_value=1000.0,
                max_daily_order_count=2,
                max_daily_notional=1000.0,
                max_total_open_order_value=1000.0,
                max_stock_position_value=2000.0,
            )
        )

        reason = trader._risk_block_reason(
            StrategyState(),
            "2026-05-01",
            "stock",
            order_value=500.0,
            current_stock_value=500.0,
        )

        self.assertIsNone(reason)

    def test_risk_status_action_reports_usage_and_limits(self) -> None:
        state = StrategyState(
            pending_orders=[
                {"createdDate": "2026-05-01", "orderValue": 250.0},
            ]
        )
        trader = make_trader(
            LeapsStrategyConfig(
                max_single_order_value=1000.0,
                max_daily_order_count=2,
                max_daily_notional=1500.0,
            )
        )

        action = trader._risk_status_action(
            state,
            "2026-05-01",
            {"position": 3},
            option_value=125.0,
            underlying_price=100.0,
        )

        self.assertEqual(action["type"], "risk")
        self.assertEqual(action["daily_order_count"], 1)
        self.assertEqual(action["daily_notional"], 250.0)
        self.assertEqual(action["total_open_order_value"], 250.0)
        self.assertEqual(action["stock_position_value"], 300.0)
        self.assertEqual(action["option_position_value"], 125.0)
        self.assertEqual(action["limits"]["max_single_order_value"], 1000.0)

    def test_order_usage_helpers(self) -> None:
        state = StrategyState(
            pending_orders=[
                {"createdDate": "2026-05-01", "quantity": 2, "limitPrice": 10},
                {
                    "createdDate": "2026-05-01",
                    "quantity": 1,
                    "limitPrice": 2,
                    "secType": "OPT",
                },
                {"createdDate": "2026-05-02", "orderValue": 999},
            ],
            completed_orders=[
                {"createdDate": "2026-05-01", "orderValue": 5},
            ],
        )

        self.assertEqual(daily_order_usage(state, "2026-05-01"), {"count": 3, "notional": 225.0})
        self.assertEqual(total_open_order_value(state), 1219.0)
        self.assertEqual(underlying_position_value({"marketValue": 12.5}, 100.0), 12.5)
        self.assertEqual(underlying_position_value({"position": 2}, 100.0), 200.0)

    def test_bid_ask_spread_pct(self) -> None:
        self.assertAlmostEqual(
            bid_ask_spread_pct({"bid": 1.0, "ask": 1.2}) or 0.0,
            0.181818,
            places=6,
        )
        self.assertIsNone(bid_ask_spread_pct({"bid": 1.2, "ask": 1.0}))


if __name__ == "__main__":
    unittest.main()
