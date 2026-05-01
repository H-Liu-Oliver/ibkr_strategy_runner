from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Quote


@dataclass
class SimulatedIBKRClient:
    account: str = "DU123456"
    symbol: str = "QQQ"
    bars: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"date": "2026-04-30", "close": 100.0},
            {"date": "2026-05-01", "close": 101.0},
        ]
    )
    quote: Quote = field(
        default_factory=lambda: Quote(
            symbol="QQQ",
            bid=99.5,
            ask=100.5,
            last=100.0,
            close=100.0,
            market_data_type=3,
        )
    )
    net_liquidation: float = 10000.0
    available_funds: float = 10000.0
    buying_power: float = 40000.0
    portfolio: list[dict[str, Any]] = field(default_factory=list)
    open_order_rows: list[dict[str, Any]] = field(default_factory=list)
    execution_rows: list[dict[str, Any]] = field(default_factory=list)
    next_order_id: int = 100

    def resolve_account(self) -> str:
        return self.account

    def require_trading_account(
        self,
        account: str,
        strategy_capital_limit: float | None = None,
        require_cap: bool = False,
    ) -> None:
        return None

    def historical_daily_bars(
        self,
        symbol: str,
        duration: str,
        primary_exchange: str | None = None,
    ) -> list[dict[str, Any]]:
        return list(self.bars)

    def account_summary(self, account: str) -> list[dict[str, Any]]:
        return [
            {"tag": "NetLiquidation", "value": str(self.net_liquidation)},
            {"tag": "AvailableFunds", "value": str(self.available_funds)},
            {"tag": "BuyingPower", "value": str(self.buying_power)},
        ]

    def positions(self, account: str | None = None) -> list[dict[str, Any]]:
        return list(self.portfolio)

    def open_orders(self) -> list[dict[str, Any]]:
        return [dict(order) for order in self.open_order_rows]

    def execution_reports(
        self,
        account: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.execution_rows
        if account:
            rows = [row for row in rows if row.get("account", account) == account]
        if symbol:
            rows = [row for row in rows if row.get("symbol", symbol.upper()) == symbol.upper()]
        return [dict(row) for row in rows]

    def snapshot_quote(
        self,
        symbol: str,
        primary_exchange: str | None = None,
        timeout: int = 15,
    ) -> Quote:
        return self.quote

    def place_stock_limit_order(
        self,
        account: str,
        symbol: str,
        action: str,
        quantity: float,
        limit_price: float,
        primary_exchange: str | None = None,
        order_ref: str | None = None,
        strategy_capital_limit: float | None = None,
    ) -> dict[str, Any]:
        return self._place_order(symbol, "STK", action, quantity, limit_price, order_ref)

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
        symbol = getattr(contract, "symbol", self.symbol)
        local_symbol = getattr(contract, "localSymbol", "")
        return self._place_order(
            symbol,
            "OPT",
            action,
            quantity,
            limit_price,
            order_ref,
            local_symbol=local_symbol,
        )

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        for order in self.open_order_rows:
            if order.get("orderId") == order_id:
                order["status"] = "Cancelled"
                order["remaining"] = 0
                return {"orderId": order_id, "status": "Cancelled", "remaining": 0}
        raise RuntimeError(f"Open order {order_id} was not found.")

    def add_open_order(
        self,
        order_id: int,
        status: str = "Submitted",
        filled: float = 0.0,
        remaining: float = 1.0,
        order_ref: str = "ibkr-strategy-runner:leaps:dca",
    ) -> None:
        self.open_order_rows.append(
            {
                "orderId": order_id,
                "permId": order_id + 1000,
                "account": self.account,
                "symbol": self.symbol,
                "secType": "STK",
                "action": "BUY",
                "quantity": filled + remaining,
                "limitPrice": 100.0,
                "orderRef": order_ref,
                "status": status,
                "filled": filled,
                "remaining": remaining,
            }
        )

    def add_execution(self, order_id: int, shares: float = 1.0, price: float = 100.0) -> None:
        self.execution_rows.append(
            {
                "time": "2026-05-01T14:30:00+00:00",
                "account": self.account,
                "orderId": order_id,
                "permId": order_id + 1000,
                "execId": f"sim-{order_id}",
                "orderRef": "ibkr-strategy-runner:leaps:dca",
                "symbol": self.symbol,
                "localSymbol": self.symbol,
                "secType": "STK",
                "side": "BOT",
                "shares": shares,
                "price": price,
                "avgPrice": price,
                "cumQty": shares,
                "exchange": "SIM",
                "commission": 0.0,
                "commissionCurrency": "USD",
                "realizedPNL": 0.0,
            }
        )

    def _place_order(
        self,
        symbol: str,
        sec_type: str,
        action: str,
        quantity: float,
        limit_price: float,
        order_ref: str | None,
        local_symbol: str = "",
    ) -> dict[str, Any]:
        order_id = self.next_order_id
        self.next_order_id += 1
        row = {
            "orderId": order_id,
            "permId": order_id + 1000,
            "account": self.account,
            "symbol": symbol.upper(),
            "localSymbol": local_symbol,
            "secType": sec_type,
            "action": action,
            "quantity": quantity,
            "limitPrice": limit_price,
            "orderRef": order_ref or "",
            "status": "Submitted",
            "filled": 0.0,
            "remaining": quantity,
        }
        self.open_order_rows.append(row)
        return dict(row)
