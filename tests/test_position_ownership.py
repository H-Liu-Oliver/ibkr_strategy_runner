from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ibkr_strategy_runner.cli import (
    cmd_bot_positions,
    cmd_import_position,
    cmd_quarantine_position,
)
from ibkr_strategy_runner.config import Settings
from ibkr_strategy_runner.leaps_strategy import CycleResult, LeapsStrategyConfig, LeapsTrader
from ibkr_strategy_runner.live_state import ManagedOptionPosition, StrategyState


class FakeExitClient:
    def __init__(self) -> None:
        self.sell_orders: list[dict[str, Any]] = []

    def snapshot_option_quote_by_con_id(self, con_id: int, timeout: int = 15) -> dict[str, Any]:
        return {"bid": 2.0, "ask": 2.0}

    def qualify_contract_by_con_id(self, con_id: int) -> object:
        return object()

    def place_contract_limit_order(
        self,
        account: str,
        contract: object,
        action: str,
        quantity: float,
        limit_price: float,
        order_ref: str | None = None,
        strategy_capital_limit: float | None = None,
    ) -> dict[str, Any]:
        order = {
            "orderId": 50,
            "permId": 5000,
            "action": action,
            "quantity": quantity,
            "limitPrice": limit_price,
            "orderRef": order_ref,
            "status": "Submitted",
        }
        self.sell_orders.append(order)
        return order


def make_settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=4002,
        client_id=201,
        account="DU123456",
        allow_order=True,
        allow_live_trading=False,
        live_account_allowlist=(),
        default_exchange="SMART",
        default_currency="USD",
        connect_timeout=10.0,
        request_timeout=15.0,
        market_data_type=3,
    )


def make_position(status: str = "OPEN", source: str = "bot") -> ManagedOptionPosition:
    return ManagedOptionPosition(
        symbol="QQQ",
        con_id=123,
        local_symbol="QQQ 20270115C00500000",
        expiry="20270115",
        strike=500.0,
        right="C",
        multiplier=100,
        quantity=1,
        entry_date="2025-01-01",
        entry_price=1.0,
        status=status,
        source=source,
    )


class PositionOwnershipTest(unittest.TestCase):
    def test_manual_positions_not_in_state_are_ignored(self) -> None:
        client = FakeExitClient()
        trader = LeapsTrader(
            client,
            LeapsStrategyConfig(strategy_capital_limit=10000),
            state_store=object(),
            execute=True,
        )
        state = StrategyState(account="DU123456", symbol="QQQ")
        result = CycleResult(date="2026-05-01", mode="execute", account="DU123456", symbol="QQQ")

        trader._manage_exits(state, result)

        self.assertEqual(client.sell_orders, [])
        self.assertEqual(result.actions, [])

    def test_imported_open_position_is_managed(self) -> None:
        client = FakeExitClient()
        trader = LeapsTrader(
            client,
            LeapsStrategyConfig(strategy_capital_limit=10000),
            state_store=object(),
            execute=True,
        )
        state = StrategyState(
            account="DU123456",
            symbol="QQQ",
            positions=[make_position(source="imported")],
        )
        result = CycleResult(date="2026-05-01", mode="execute", account="DU123456", symbol="QQQ")

        trader._manage_exits(state, result)

        self.assertEqual(client.sell_orders[0]["action"], "SELL")
        self.assertEqual(state.positions[0].status, "CLOSE_SUBMITTED")
        self.assertEqual(state.pending_orders[0].type, "option-exit")

    def test_quarantined_position_is_not_managed(self) -> None:
        client = FakeExitClient()
        trader = LeapsTrader(
            client,
            LeapsStrategyConfig(strategy_capital_limit=10000),
            state_store=object(),
            execute=True,
        )
        state = StrategyState(
            account="DU123456",
            symbol="QQQ",
            positions=[make_position(status="QUARANTINED")],
        )
        result = CycleResult(date="2026-05-01", mode="execute", account="DU123456", symbol="QQQ")

        trader._manage_exits(state, result)

        self.assertEqual(client.sell_orders, [])
        self.assertEqual(result.actions, [])

    def test_import_and_quarantine_position_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_args = {
                "config": None,
                "state_dir": Path(tmp),
            }
            import_result = cmd_import_position(
                make_settings(),
                argparse.Namespace(
                    **base_args,
                    con_id=123,
                    local_symbol="QQQ 20270115C00500000",
                    expiry="20270115",
                    strike=500.0,
                    right="C",
                    quantity=1,
                    entry_price=1.0,
                    entry_date="2025-01-01",
                    multiplier=100,
                ),
            )

            self.assertEqual(import_result["imported"]["source"], "imported")
            positions_result = cmd_bot_positions(
                make_settings(),
                argparse.Namespace(**base_args),
            )
            self.assertEqual(positions_result["positions"][0]["status"], "OPEN")

            quarantine_result = cmd_quarantine_position(
                make_settings(),
                argparse.Namespace(
                    **base_args,
                    con_id=123,
                    local_symbol=None,
                ),
            )

            self.assertEqual(quarantine_result["quarantined"]["status"], "QUARANTINED")
            positions_result = cmd_bot_positions(
                make_settings(),
                argparse.Namespace(**base_args),
            )
            self.assertEqual(positions_result["positions"][0]["status"], "QUARANTINED")


if __name__ == "__main__":
    unittest.main()
