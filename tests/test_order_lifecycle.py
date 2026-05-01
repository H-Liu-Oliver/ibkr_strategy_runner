from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ibkr_strategy_runner.leaps_strategy import CycleResult, LeapsStrategyConfig, LeapsTrader
from ibkr_strategy_runner.live_state import ManagedOrder, StateStore, StrategyState


class FakeReconcileClient:
    def __init__(
        self,
        open_orders: list[dict[str, Any]] | None = None,
        executions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._open_orders = open_orders or []
        self._executions = executions or []

    def positions(self, account: str) -> list[dict[str, Any]]:
        return []

    def open_orders(self) -> list[dict[str, Any]]:
        return self._open_orders

    def execution_reports(self, account: str, symbol: str) -> list[dict[str, Any]]:
        return self._executions


def make_order(**overrides: Any) -> ManagedOrder:
    payload = {
        "type": "dca",
        "symbol": "QQQ",
        "action": "BUY",
        "quantity": 3,
        "sec_type": "STK",
        "order_id": 10,
        "limit_price": 100.0,
        "order_ref": "ibkr-strategy-runner:leaps:dca",
        "lifecycle_state": "planned",
        "created_date": "2026-05-01",
    }
    payload.update(overrides)
    return ManagedOrder(**payload)


class ManagedOrderLifecycleTest(unittest.TestCase):
    def test_order_transition_rules(self) -> None:
        order = make_order()

        order.transition_to("submitted", broker_status="Submitted")
        order.transition_to("pre_submitted", broker_status="PreSubmitted")
        order.transition_to("partially_filled", filled=1, remaining=2)
        order.transition_to("filled", filled=3, remaining=0)

        self.assertEqual(order.lifecycle_state, "filled")
        with self.assertRaisesRegex(ValueError, "Invalid order lifecycle transition"):
            order.transition_to("submitted")

    def test_state_serialization_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp), "leaps-overlay", "DU123456", "QQQ")
            state = StrategyState(
                strategy_name="leaps-overlay",
                account="DU123456",
                symbol="QQQ",
                pending_orders=[
                    make_order(
                        lifecycle_state="pre_submitted",
                        broker_status="PreSubmitted",
                        remaining=3,
                    )
                ],
                completed_orders=[
                    make_order(
                        order_id=11,
                        lifecycle_state="filled",
                        broker_status="Filled",
                        filled=3,
                        remaining=0,
                        fills=[{"execId": "abc"}],
                    )
                ],
            )

            store.save(state)
            loaded = store.load()

            self.assertIsInstance(loaded.pending_orders[0], ManagedOrder)
            self.assertEqual(loaded.pending_orders[0].lifecycle_state, "pre_submitted")
            self.assertEqual(loaded.pending_orders[0].broker_status, "PreSubmitted")
            self.assertEqual(loaded.completed_orders[0].lifecycle_state, "filled")
            self.assertEqual(loaded.completed_orders[0].fills, [{"execId": "abc"}])

    def test_loads_legacy_order_dictionaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp), "leaps-overlay", "DU123456", "QQQ")
            store.state_path.write_text(
                json.dumps(
                    {
                        "strategy_name": "leaps-overlay",
                        "account": "DU123456",
                        "symbol": "QQQ",
                        "pending_orders": [
                            {
                                "type": "dca",
                                "orderId": 12,
                                "symbol": "QQQ",
                                "secType": "STK",
                                "action": "BUY",
                                "quantity": 3,
                                "limitPrice": 100.0,
                                "status": "PreSubmitted",
                                "createdDate": "2026-05-01",
                            }
                        ],
                        "completed_orders": [
                            {
                                "type": "dca",
                                "orderId": 13,
                                "symbol": "QQQ",
                                "secType": "STK",
                                "action": "BUY",
                                "quantity": 3,
                                "status": "CLEARED_NO_FILL_SEEN",
                                "clearedDate": "2026-05-01",
                            }
                        ],
                    }
                )
            )

            loaded = store.load()

            self.assertEqual(loaded.pending_orders[0].order_id, 12)
            self.assertEqual(loaded.pending_orders[0].lifecycle_state, "pre_submitted")
            self.assertEqual(loaded.completed_orders[0].lifecycle_state, "unknown")

    def test_reconcile_marks_stale_pending_order_unknown(self) -> None:
        trader = LeapsTrader(
            FakeReconcileClient(),
            LeapsStrategyConfig(),
            state_store=object(),
        )
        state = StrategyState(
            account="DU123456",
            symbol="QQQ",
            pending_orders=[make_order(lifecycle_state="submitted")],
        )
        result = CycleResult(date="2026-05-01", mode="reconcile", account="DU123456", symbol="QQQ")

        trader._reconcile_submitted_positions(state, result)

        self.assertEqual(state.pending_orders, [])
        self.assertEqual(state.completed_orders[0].lifecycle_state, "unknown")
        self.assertEqual(result.actions[0]["action"], "PENDING_ORDER_CLEARED")
        self.assertIn("check manually", result.actions[0]["reason"])

    def test_reconcile_keeps_unknown_open_order_pending(self) -> None:
        trader = LeapsTrader(
            FakeReconcileClient(
                open_orders=[
                    {
                        "orderId": 10,
                        "symbol": "QQQ",
                        "secType": "STK",
                        "action": "BUY",
                        "quantity": 3,
                        "limitPrice": 100.0,
                        "orderRef": "ibkr-strategy-runner:leaps:dca",
                        "status": "MysteryStatus",
                        "filled": 0,
                        "remaining": 3,
                    }
                ]
            ),
            LeapsStrategyConfig(),
            state_store=object(),
        )
        state = StrategyState(
            account="DU123456",
            symbol="QQQ",
            pending_orders=[make_order(lifecycle_state="submitted")],
        )
        result = CycleResult(date="2026-05-01", mode="reconcile", account="DU123456", symbol="QQQ")

        trader._reconcile_submitted_positions(state, result)

        self.assertEqual(len(state.pending_orders), 1)
        self.assertEqual(state.pending_orders[0].lifecycle_state, "unknown")
        self.assertEqual(result.actions[0]["action"], "CHECK_ORDER_STATUS")


if __name__ == "__main__":
    unittest.main()
