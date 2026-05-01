from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .live_state import STATE_VERSION, StateStore, StrategyState, state_from_dict, utc_now_iso

SQLITE_SCHEMA_VERSION = 1


class SQLiteStateStore:
    def __init__(self, state_dir: Path, strategy_name: str, account: str, symbol: str) -> None:
        safe_account = account.replace("/", "_").replace(" ", "_")
        safe_symbol = symbol.upper().replace("/", "_").replace(".", "_")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / f"{strategy_name}_{safe_account}_{safe_symbol}.sqlite3"
        self.state_path = self.db_path
        self.journal_path = self.db_path
        self.strategy_name = strategy_name
        self.account = account
        self.symbol = symbol.upper()
        self._ensure_schema()

    def load(self) -> StrategyState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM strategy_state
                WHERE strategy_name = ? AND account = ? AND symbol = ?
                """,
                (self.strategy_name, self.account, self.symbol),
            ).fetchone()
        if row is None:
            return StrategyState(
                strategy_name=self.strategy_name,
                account=self.account,
                symbol=self.symbol,
            )
        return state_from_dict(json.loads(row["payload_json"]), self.strategy_name, self.account, self.symbol)

    def save(self, state: StrategyState) -> None:
        state.updated_at = utc_now_iso()
        payload_json = json.dumps(asdict(state), sort_keys=True)
        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO strategy_state (
                        strategy_name, account, symbol, version, updated_at, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(strategy_name, account, symbol) DO UPDATE SET
                        version = excluded.version,
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    (
                        state.strategy_name,
                        state.account,
                        state.symbol,
                        state.version,
                        state.updated_at,
                        payload_json,
                    ),
                )

    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO journal (
                        ts, strategy_name, account, symbol, event_type, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        utc_now_iso(),
                        self.strategy_name,
                        self.account,
                        self.symbol,
                        event_type,
                        json.dumps(payload, sort_keys=True),
                    ),
                )

    def journal_events(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ts, event_type, payload_json
                FROM journal
                WHERE strategy_name = ? AND account = ? AND symbol = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.strategy_name, self.account, self.symbol, limit),
            ).fetchall()
        events = [
            {
                "ts": row["ts"],
                "event": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in reversed(rows)
        ]
        return events

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS strategy_state (
                        strategy_name TEXT NOT NULL,
                        account TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (strategy_name, account, symbol)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        strategy_name TEXT NOT NULL,
                        account TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO metadata (key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(SQLITE_SCHEMA_VERSION),),
                )


def migrate_json_state_to_sqlite(json_store: StateStore, sqlite_store: SQLiteStateStore) -> dict[str, Any]:
    state = json_store.load()
    sqlite_store.save(state)
    migrated_events = 0
    for event in json_store.journal_events(limit=1_000_000):
        sqlite_store.record_event(str(event.get("event") or "event"), dict(event.get("payload") or {}))
        migrated_events += 1
    return {
        "statePath": str(sqlite_store.state_path),
        "sourceStatePath": str(json_store.state_path),
        "version": STATE_VERSION,
        "journalEventsMigrated": migrated_events,
    }
