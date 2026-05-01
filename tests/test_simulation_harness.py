from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ibkr_strategy_runner.leaps_strategy import LeapsStrategyConfig, LeapsTrader
from ibkr_strategy_runner.live_state import ManagedOrder, StateStore, StrategyState
from ibkr_strategy_runner.simulation import SimulatedIBKRClient


def make_store(tmp: str) -> StateStore:
    return StateStore(Path(tmp), "leaps-overlay", "DU123456", "QQQ")


def make_pending_order() -> ManagedOrder:
    return ManagedOrder(
        type="dca",
        symbol="QQQ",
        action="BUY",
        quantity=3,
        sec_type="STK",
        order_id=10,
        limit_price=100.0,
        order_value=300.0,
        order_ref="ibkr-strategy-runner:leaps:dca",
        lifecycle_state="submitted",
        created_date="2026-05-01",
    )


class SimulationHarnessTest(unittest.TestCase):
    def test_restart_with_pending_order_keeps_it_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.save(StrategyState(account="DU123456", symbol="QQQ", pending_orders=[make_pending_order()]))
            client = SimulatedIBKRClient()
            client.add_open_order(10, status="Submitted", remaining=3)

            result = LeapsTrader(client, LeapsStrategyConfig(), store).reconcile_state()
            state = store.load()

            self.assertEqual(state.pending_orders[0].lifecycle_state, "submitted")
            self.assertEqual(result.actions[-1]["action"], "SUMMARY")

    def test_fill_after_restart_moves_order_to_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.save(StrategyState(account="DU123456", symbol="QQQ", pending_orders=[make_pending_order()]))
            client = SimulatedIBKRClient()
            client.add_execution(10, shares=3)

            LeapsTrader(client, LeapsStrategyConfig(), store).reconcile_state()
            state = store.load()

            self.assertEqual(state.pending_orders, [])
            self.assertEqual(state.completed_orders[0].lifecycle_state, "filled")

    def test_partial_fill_stays_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.save(StrategyState(account="DU123456", symbol="QQQ", pending_orders=[make_pending_order()]))
            client = SimulatedIBKRClient()
            client.add_open_order(10, status="Submitted", filled=1, remaining=2)

            LeapsTrader(client, LeapsStrategyConfig(), store).reconcile_state()
            state = store.load()

            self.assertEqual(state.pending_orders[0].lifecycle_state, "partially_filled")

    def test_rejected_and_expired_orders_become_completed(self) -> None:
        cases = (("Inactive", "rejected"), ("Expired", "expired"))
        for broker_status, lifecycle_state in cases:
            with self.subTest(broker_status=broker_status):
                with tempfile.TemporaryDirectory() as tmp:
                    store = make_store(tmp)
                    store.save(
                        StrategyState(
                            account="DU123456",
                            symbol="QQQ",
                            pending_orders=[make_pending_order()],
                        )
                    )
                    client = SimulatedIBKRClient()
                    client.add_open_order(10, status=broker_status, remaining=3)

                    LeapsTrader(client, LeapsStrategyConfig(), store).reconcile_state()
                    state = store.load()

                    self.assertEqual(state.pending_orders, [])
                    self.assertEqual(state.completed_orders[0].lifecycle_state, lifecycle_state)

    def test_risk_limit_block_is_simulated_without_ibkr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            client = SimulatedIBKRClient()
            trader = LeapsTrader(
                client,
                LeapsStrategyConfig(
                    dca_months=1,
                    min_stock_order_dollars=1,
                    max_single_order_value=1,
                ),
                store,
            )

            result = trader.run_daily_cycle()

            reasons = [action.get("reason", "") for action in result.actions]
            self.assertTrue(any("max_single_order_value" in reason for reason in reasons))

    def test_duplicate_cycle_prevention_is_simulated_without_ibkr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            client = SimulatedIBKRClient()
            trader = LeapsTrader(
                client,
                LeapsStrategyConfig(dca_months=1, min_stock_order_dollars=1),
                store,
            )

            first = trader.run_daily_cycle()
            second = trader.run_daily_cycle()

            self.assertFalse(first.skipped)
            self.assertTrue(second.skipped)
            self.assertIn("cycle already completed", second.reason or "")


if __name__ == "__main__":
    unittest.main()
