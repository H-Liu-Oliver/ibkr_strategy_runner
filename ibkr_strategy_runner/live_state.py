from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


STATE_VERSION = 1

ORDER_LIFECYCLE_STATES = frozenset(
    {
        "planned",
        "submitted",
        "pre_submitted",
        "partially_filled",
        "filled",
        "cancelled",
        "expired",
        "rejected",
        "unknown",
    }
)
TERMINAL_ORDER_STATES = frozenset({"filled", "cancelled", "expired", "rejected"})
ORDER_TRANSITIONS = {
    "planned": frozenset({"submitted", "pre_submitted", "cancelled", "rejected", "unknown"}),
    "submitted": frozenset(
        {
            "submitted",
            "pre_submitted",
            "partially_filled",
            "filled",
            "cancelled",
            "expired",
            "rejected",
            "unknown",
        }
    ),
    "pre_submitted": frozenset(
        {
            "submitted",
            "pre_submitted",
            "partially_filled",
            "filled",
            "cancelled",
            "expired",
            "rejected",
            "unknown",
        }
    ),
    "partially_filled": frozenset(
        {"partially_filled", "filled", "cancelled", "expired", "rejected", "unknown"}
    ),
    "filled": frozenset({"filled"}),
    "cancelled": frozenset({"cancelled"}),
    "expired": frozenset({"expired"}),
    "rejected": frozenset({"rejected"}),
    "unknown": frozenset(
        {
            "submitted",
            "pre_submitted",
            "partially_filled",
            "filled",
            "cancelled",
            "expired",
            "rejected",
            "unknown",
        }
    ),
}


def normalize_order_state(value: str | None) -> str:
    normalized = (value or "unknown").strip().lower().replace("-", "_")
    if normalized in ORDER_LIFECYCLE_STATES:
        return normalized
    return "unknown"


def lifecycle_from_broker_status(
    broker_status: str | None,
    filled: float | None = None,
    remaining: float | None = None,
) -> str:
    filled_qty = float(filled or 0.0)
    remaining_qty = float(remaining) if remaining is not None else None
    if remaining_qty is not None and filled_qty > 0 and remaining_qty > 0:
        return "partially_filled"
    if filled_qty > 0 and remaining_qty == 0:
        return "filled"

    normalized = (broker_status or "").strip().lower().replace("_", "")
    if normalized in {"filled"}:
        return "filled"
    if normalized in {"cancelled", "apicancelled"}:
        return "cancelled"
    if normalized in {"inactive", "validationerror"}:
        return "rejected"
    if normalized in {"presubmitted"}:
        return "pre_submitted"
    if normalized in {"submitted", "pendingsubmit", "apipending", "pendingcancel"}:
        return "submitted"
    if normalized in {"expired"}:
        return "expired"
    return "unknown"


def _first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


@dataclass
class ManagedOrder:
    type: str
    symbol: str
    action: str
    quantity: float
    sec_type: str
    order_id: int | None = None
    perm_id: int | None = None
    local_symbol: str = ""
    limit_price: float | None = None
    multiplier: float | None = None
    order_value: float | None = None
    order_ref: str = ""
    lifecycle_state: str = "planned"
    broker_status: str | None = None
    filled: float = 0.0
    remaining: float | None = None
    created_date: str | None = None
    updated_at: str | None = None
    cleared_date: str | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lifecycle_state = normalize_order_state(self.lifecycle_state)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ManagedOrder":
        broker_status = _first_present(raw, "broker_status", "status")
        filled = float(_first_present(raw, "filled") or 0.0)
        remaining = _first_present(raw, "remaining")
        if "lifecycle_state" in raw:
            lifecycle_state = normalize_order_state(raw.get("lifecycle_state"))
        elif raw.get("status") == "FILLED_OR_CLEARED":
            lifecycle_state = "filled"
        elif raw.get("status") == "CLEARED_NO_FILL_SEEN":
            lifecycle_state = "unknown"
        else:
            lifecycle_state = lifecycle_from_broker_status(broker_status, filled, remaining)

        return cls(
            type=str(raw.get("type") or "managed"),
            symbol=str(raw.get("symbol") or "").upper(),
            action=str(raw.get("action") or "").upper(),
            quantity=float(raw.get("quantity") or 0.0),
            sec_type=str(_first_present(raw, "sec_type", "secType") or ""),
            order_id=_optional_int(_first_present(raw, "order_id", "orderId")),
            perm_id=_optional_int(_first_present(raw, "perm_id", "permId")),
            local_symbol=str(_first_present(raw, "local_symbol", "localSymbol") or ""),
            limit_price=_optional_float(_first_present(raw, "limit_price", "limitPrice")),
            multiplier=_optional_float(raw.get("multiplier")),
            order_value=_optional_float(_first_present(raw, "order_value", "orderValue")),
            order_ref=str(_first_present(raw, "order_ref", "orderRef") or ""),
            lifecycle_state=lifecycle_state,
            broker_status=broker_status,
            filled=filled,
            remaining=_optional_float(remaining),
            created_date=_first_present(raw, "created_date", "createdDate"),
            updated_at=raw.get("updated_at"),
            cleared_date=_first_present(raw, "cleared_date", "clearedDate"),
            fills=list(raw.get("fills") or []),
            notes=list(raw.get("notes") or []),
        )

    @classmethod
    def from_broker_open_order(
        cls,
        raw: dict[str, Any],
        order_type: str = "managed",
    ) -> "ManagedOrder":
        order = cls.from_dict(
            {
                "type": order_type,
                "orderId": raw.get("orderId"),
                "permId": raw.get("permId"),
                "symbol": raw.get("symbol"),
                "localSymbol": raw.get("localSymbol"),
                "secType": raw.get("secType"),
                "action": raw.get("action"),
                "quantity": raw.get("quantity"),
                "limitPrice": raw.get("limitPrice"),
                "orderRef": raw.get("orderRef"),
                "status": raw.get("status"),
                "remaining": raw.get("remaining"),
                "filled": raw.get("filled"),
            }
        )
        order.sync_from_broker(raw)
        return order

    def transition_to(
        self,
        next_state: str,
        broker_status: str | None = None,
        filled: float | None = None,
        remaining: float | None = None,
        note: str | None = None,
    ) -> None:
        normalized = normalize_order_state(next_state)
        allowed = ORDER_TRANSITIONS[self.lifecycle_state]
        if normalized != self.lifecycle_state and normalized not in allowed:
            raise ValueError(
                f"Invalid order lifecycle transition "
                f"{self.lifecycle_state!r} -> {normalized!r}."
            )
        self.lifecycle_state = normalized
        if broker_status is not None:
            self.broker_status = broker_status
        if filled is not None:
            self.filled = float(filled)
        if remaining is not None:
            self.remaining = float(remaining)
        if note:
            self.notes.append(note)
        self.updated_at = utc_now_iso()

    def sync_from_broker(self, raw: dict[str, Any]) -> None:
        if raw.get("orderId") is not None:
            self.order_id = int(raw["orderId"])
        if raw.get("permId") is not None:
            self.perm_id = int(raw["permId"])
        if raw.get("orderRef"):
            self.order_ref = str(raw["orderRef"])
        if raw.get("status") is not None:
            self.broker_status = str(raw["status"])
        if raw.get("filled") is not None:
            self.filled = float(raw["filled"])
        if raw.get("remaining") is not None:
            self.remaining = float(raw["remaining"])
        if raw.get("limitPrice") is not None:
            self.limit_price = _optional_float(raw.get("limitPrice"))
        if raw.get("quantity") is not None:
            self.quantity = float(raw["quantity"])
        self.transition_to(
            lifecycle_from_broker_status(
                self.broker_status,
                self.filled,
                self.remaining,
            )
        )

    def mark_cleared(
        self,
        fills: list[dict[str, Any]],
        cleared_date: str,
    ) -> None:
        self.fills = fills
        self.cleared_date = cleared_date
        if fills:
            self.transition_to(
                "filled",
                note="order is no longer open at IBKR and fill reports were found",
            )
        elif self.lifecycle_state in TERMINAL_ORDER_STATES:
            self.transition_to(
                self.lifecycle_state,
                note="order is no longer open at IBKR and was already terminal",
            )
        else:
            self.transition_to(
                "unknown",
                note="order is no longer open at IBKR and no fill report was returned",
            )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


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
    pending_orders: list[ManagedOrder] = field(default_factory=list)
    completed_orders: list[ManagedOrder] = field(default_factory=list)

    def open_positions(self) -> list[ManagedOptionPosition]:
        return [pos for pos in self.positions if pos.status == "OPEN"]

    def submitted_positions(self) -> list[ManagedOptionPosition]:
        return [pos for pos in self.positions if pos.status == "SUBMITTED"]

    def __post_init__(self) -> None:
        self.pending_orders = [
            order if isinstance(order, ManagedOrder) else ManagedOrder.from_dict(order)
            for order in self.pending_orders
        ]
        self.completed_orders = [
            order if isinstance(order, ManagedOrder) else ManagedOrder.from_dict(order)
            for order in self.completed_orders
        ]


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
            pending_orders=[ManagedOrder.from_dict(item) for item in raw.get("pending_orders", [])],
            completed_orders=[
                ManagedOrder.from_dict(item)
                for item in raw.get("completed_orders", [])
            ],
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
