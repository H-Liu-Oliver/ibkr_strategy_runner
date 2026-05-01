from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ibkr_strategy_runner.live_state import ManagedOrder, StateStore, StrategyState
from ibkr_strategy_runner.sqlite_state import (
    SQLITE_SCHEMA_VERSION,
    SQLiteStateStore,
    migrate_json_state_to_sqlite,
)


def make_state() -> StrategyState:
    return StrategyState(
        strategy_name="leaps-overlay",
        account="DU123456",
        symbol="QQQ",
        last_cycle_date="2026-05-01",
        pending_orders=[
            ManagedOrder(
                type="dca",
                symbol="QQQ",
                action="BUY",
                quantity=1,
                sec_type="STK",
                order_id=1,
                order_value=100.0,
                lifecycle_state="submitted",
                created_date="2026-05-01",
            )
        ],
    )


class SQLiteStateStoreTest(unittest.TestCase):
    def test_sqlite_state_round_trip_and_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp), "leaps-overlay", "DU123456", "QQQ")

            store.save(make_state())
            loaded = store.load()

            self.assertEqual(loaded.last_cycle_date, "2026-05-01")
            self.assertEqual(loaded.pending_orders[0].order_id, 1)
            with sqlite3.connect(store.db_path) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, str(SQLITE_SCHEMA_VERSION))

    def test_sqlite_journal_events_are_ordered_and_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp), "leaps-overlay", "DU123456", "QQQ")

            store.record_event("first", {"value": 1})
            store.record_event("second", {"value": 2})

            events = store.journal_events(limit=1)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "second")
            self.assertEqual(events[0]["payload"], {"value": 2})

    def test_migrates_json_state_and_journal_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            json_store = StateStore(state_dir, "leaps-overlay", "DU123456", "QQQ")
            sqlite_store = SQLiteStateStore(state_dir, "leaps-overlay", "DU123456", "QQQ")
            json_store.save(make_state())
            json_store.record_event("cycle", {"ok": True})

            result = migrate_json_state_to_sqlite(json_store, sqlite_store)
            loaded = sqlite_store.load()
            events = sqlite_store.journal_events(limit=10)

            self.assertEqual(result["journalEventsMigrated"], 1)
            self.assertEqual(loaded.pending_orders[0].order_id, 1)
            self.assertEqual(events[0]["event"], "cycle")
            self.assertEqual(events[0]["payload"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
