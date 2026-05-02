from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from ibkr_strategy_runner.cli import (
    cmd_doctor,
    cmd_fills,
    cmd_journal,
    cmd_resolve_order,
    cmd_risk,
    cmd_status,
)
from ibkr_strategy_runner.config import Settings
from ibkr_strategy_runner.live_state import ManagedOrder, StateStore, StrategyState


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


def make_args(state_dir: Path, **overrides: object) -> argparse.Namespace:
    payload = {
        "config": None,
        "state_dir": state_dir,
        "date": "2026-05-01",
        "limit": 10,
        "skip_ibkr": True,
        "service_unit": state_dir / "ibkr-strategy-runner-leaps.service",
    }
    payload.update(overrides)
    return argparse.Namespace(**payload)


def seed_state(state_dir: Path) -> StateStore:
    store = StateStore(state_dir, "leaps-overlay", "DU123456", "QQQ")
    store.save(
        StrategyState(
            strategy_name="leaps-overlay",
            account="DU123456",
            symbol="QQQ",
            pending_orders=[
                ManagedOrder(
                    type="dca",
                    symbol="QQQ",
                    action="BUY",
                    quantity=1,
                    sec_type="STK",
                    order_id=1,
                    limit_price=100.0,
                    order_value=100.0,
                    lifecycle_state="submitted",
                    created_date="2026-05-01",
                )
            ],
            completed_orders=[
                ManagedOrder(
                    type="dca",
                    symbol="QQQ",
                    action="BUY",
                    quantity=1,
                    sec_type="STK",
                    order_id=2,
                    limit_price=50.0,
                    order_value=50.0,
                    lifecycle_state="filled",
                    created_date="2026-05-01",
                    fills=[{"execId": "fill-1", "price": 50.0}],
                ),
                ManagedOrder(
                    type="dca",
                    symbol="QQQ",
                    action="BUY",
                    quantity=1,
                    sec_type="STK",
                    order_id=3,
                    lifecycle_state="unknown",
                    created_date="2026-04-30",
                ),
            ],
        )
    )
    store.record_event("test", {"value": 1})
    store.record_event("test", {"value": 2})
    return store


class OperatorCommandTest(unittest.TestCase):
    def test_status_reports_attention_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_state(Path(tmp))

            result = cmd_status(make_settings(), make_args(Path(tmp)))

            self.assertEqual(result["pendingOrderCount"], 1)
            self.assertEqual(result["completedOrderCount"], 2)
            self.assertEqual(result["unknownCompletedOrderCount"], 1)
            self.assertTrue(result["needsAttention"])

    def test_risk_reports_usage_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_state(Path(tmp))

            result = cmd_risk(make_settings(), make_args(Path(tmp)))

            self.assertEqual(result["usage"]["dailyOrderCount"], 2)
            self.assertEqual(result["usage"]["dailyNotional"], 150.0)
            self.assertEqual(result["usage"]["totalOpenOrderValue"], 100.0)
            self.assertIn("max_daily_notional", result["limits"])

    def test_journal_and_fills_commands_report_persisted_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_state(Path(tmp))

            journal = cmd_journal(make_settings(), make_args(Path(tmp), limit=1))
            fills = cmd_fills(make_settings(), make_args(Path(tmp)))

            self.assertEqual(len(journal["events"]), 1)
            self.assertEqual(journal["events"][0]["payload"], {"value": 2})
            self.assertEqual(fills["fills"][0]["execId"], "fill-1")
            self.assertEqual(fills["fills"][0]["orderId"], 2)

    def test_doctor_distinguishes_checks_without_ibkr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_state(Path(tmp))
            service_unit = Path(tmp) / "ibkr-strategy-runner-leaps.service"
            service_unit.write_text("[Service]\n")

            result = cmd_doctor(
                make_settings(),
                make_args(Path(tmp), service_unit=service_unit),
            )

            self.assertEqual(result["status"], "ok")
            statuses = {check["name"]: check["status"] for check in result["checks"]}
            self.assertEqual(statuses["config"], "ok")
            self.assertEqual(statuses["ibkr"], "skipped")
            self.assertEqual(statuses["state"], "ok")
            self.assertEqual(statuses["service"], "ok")

    def test_resolve_order_marks_unknown_completed_order_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_state(Path(tmp))

            result = cmd_resolve_order(
                make_settings(),
                make_args(
                    Path(tmp),
                    order_id=3,
                    state="cancelled",
                    note="verified cancelled in IBKR",
                ),
            )

            status = cmd_status(make_settings(), make_args(Path(tmp)))
            journal = cmd_journal(make_settings(), make_args(Path(tmp), limit=1))
            self.assertEqual(result["resolved"]["lifecycle_state"], "cancelled")
            self.assertEqual(status["unknownCompletedOrderCount"], 0)
            self.assertFalse(status["needsAttention"])
            self.assertEqual(journal["events"][0]["event"], "resolve-order")


if __name__ == "__main__":
    unittest.main()
