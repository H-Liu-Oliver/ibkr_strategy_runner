from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


STATE_VERSION = 1


@dataclass
class ManagedOptionPosition:
    symbol: str
    con_id: int
    local_symbol: str
    expiry: str
    strike: float
    right: str
    multiplier: int
    quantity: int
    entry_date: str
    entry_price: float
    order_id: int | None = None
    status: str = "SUBMITTED"
    close_order_id: int | None = None
    closed_date: str | None = None
    close_price: float | None = None
    close_reason: str | None = None


@dataclass
class StrategyState:
    version: int = STATE_VERSION
    strategy_name: str = "leaps-overlay"
    symbol: str = ""
    account: str = ""
    created_at: str = field(default_factory=lambda: utc_now_iso())
    updated_at: str = field(default_factory=lambda: utc_now_iso())
    last_cycle_date: str | None = None
    last_dry_run_cycle_date: str | None = None
    dca_days_completed: int = 0
    dry_run_cycles_completed: int = 0
    positions: list[ManagedOptionPosition] = field(default_factory=list)
    pending_orders: list[dict[str, Any]] = field(default_factory=list)
    completed_orders: list[dict[str, Any]] = field(default_factory=list)

    def open_positions(self) -> list[ManagedOptionPosition]:
        return [pos for pos in self.positions if pos.status == "OPEN"]

    def submitted_positions(self) -> list[ManagedOptionPosition]:
        return [pos for pos in self.positions if pos.status == "SUBMITTED"]


class StateStore:
    def __init__(self, state_dir: Path, strategy_name: str, account: str, symbol: str) -> None:
        safe_account = account.replace("/", "_").replace(" ", "_")
        safe_symbol = symbol.upper().replace("/", "_").replace(".", "_")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / f"{strategy_name}_{safe_account}_{safe_symbol}.json"
        self.journal_path = self.state_dir / f"{strategy_name}_{safe_account}_{safe_symbol}.jsonl"
        self.strategy_name = strategy_name
        self.account = account
        self.symbol = symbol.upper()

    def load(self) -> StrategyState:
        if not self.state_path.exists():
            return StrategyState(
                strategy_name=self.strategy_name,
                account=self.account,
                symbol=self.symbol,
            )

        raw = json.loads(self.state_path.read_text())
        positions = [
            ManagedOptionPosition(**item)
            for item in raw.get("positions", [])
        ]
        return StrategyState(
            version=raw.get("version", STATE_VERSION),
            strategy_name=raw.get("strategy_name", self.strategy_name),
            symbol=raw.get("symbol", self.symbol),
            account=raw.get("account", self.account),
            created_at=raw.get("created_at", utc_now_iso()),
            updated_at=raw.get("updated_at", utc_now_iso()),
            last_cycle_date=raw.get("last_cycle_date"),
            last_dry_run_cycle_date=raw.get("last_dry_run_cycle_date"),
            dca_days_completed=int(raw.get("dca_days_completed", 0)),
            dry_run_cycles_completed=int(raw.get("dry_run_cycles_completed", 0)),
            positions=positions,
            pending_orders=list(raw.get("pending_orders", [])),
            completed_orders=list(raw.get("completed_orders", [])),
        )

    def save(self, state: StrategyState) -> None:
        state.updated_at = utc_now_iso()
        payload = asdict(state)
        tmp_path = self.state_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp_path, self.state_path)

    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "ts": utc_now_iso(),
            "event": event_type,
            "payload": payload,
        }
        with self.journal_path.open("a") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return date.today().isoformat()
