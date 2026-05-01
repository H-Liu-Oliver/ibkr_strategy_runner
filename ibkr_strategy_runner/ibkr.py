from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from ib_async import Contract, ExecutionFilter, IB, LimitOrder, Stock

from .config import Settings
from .models import Quote


IB_UNSET_DOUBLE = 1.7976931348623157e308

SUMMARY_TAGS = {
    "NetLiquidation",
    "TotalCashValue",
    "BuyingPower",
    "AvailableFunds",
    "ExcessLiquidity",
    "Cushion",
}


class IBKRClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ib = IB()
        self.errors: list[tuple[int, int, str]] = []

    def __enter__(self) -> "IBKRClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.disconnect()

    def connect(self) -> None:
        self.ib.RaiseRequestErrors = True
        self.ib.RequestTimeout = self.settings.request_timeout
        self.ib.errorEvent += self._on_error
        self.ib.connect(
            self.settings.host,
            self.settings.port,
            clientId=self.settings.client_id,
            timeout=self.settings.connect_timeout,
        )

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def managed_accounts(self) -> list[str]:
        return list(self.ib.managedAccounts())

    def resolve_account(self, account: str | None = None) -> str:
        requested = account or self.settings.account
        accounts = self.managed_accounts()
        if not accounts:
            raise RuntimeError("No managed accounts returned. Check the IBKR paper login.")
        if requested:
            if requested not in accounts:
                raise RuntimeError(
                    f"Requested account {requested!r} is not in managed accounts: {accounts}"
                )
            return requested
        return accounts[0]

    def require_paper_account(self, account: str) -> None:
        if not account.startswith("DU"):
            raise RuntimeError(f"Refusing to trade on non-paper account: {account}")

    def account_summary(self, account: str) -> list[dict[str, str]]:
        rows = []
        for row in self.ib.accountSummary(account):
            if row.tag in SUMMARY_TAGS:
                rows.append(
                    {
                        "tag": row.tag,
                        "value": str(row.value),
                        "currency": str(row.currency),
                    }
                )
        return rows

    def positions(self, account: str | None = None) -> list[dict[str, Any]]:
        selected_account = account or self.settings.account
        rows = []
        for pos in self.ib.positions():
            if selected_account and pos.account != selected_account:
                continue
            contract = pos.contract
            rows.append(
                {
                    "account": pos.account,
                    "conId": getattr(contract, "conId", None),
                    "symbol": contract.symbol,
                    "localSymbol": getattr(contract, "localSymbol", ""),
                    "secType": contract.secType,
                    "right": getattr(contract, "right", ""),
                    "strike": ib_number_or_none(getattr(contract, "strike", None)),
                    "lastTradeDateOrContractMonth": getattr(
                        contract,
                        "lastTradeDateOrContractMonth",
                        "",
                    ),
                    "exchange": contract.exchange,
                    "currency": contract.currency,
                    "position": pos.position,
                    "avgCost": pos.avgCost,
                }
            )
        return rows

    def qualify_stock(
        self,
        symbol: str,
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
    ) -> Stock:
        contract = Stock(
            symbol.upper(),
            exchange or self.settings.default_exchange,
            currency or self.settings.default_currency,
            primaryExchange=primary_exchange or "",
        )
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Unable to qualify stock contract for {symbol!r}")
        return qualified[0]

    def qualify_contract_by_con_id(self, con_id: int) -> Contract:
        contract = Contract(conId=int(con_id), exchange=self.settings.default_exchange)
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Unable to qualify contract conId={con_id}")
        return qualified[0]

    def option_chains(
        self,
        symbol: str,
        underlying_con_id: int,
        exchange: str = "",
        underlying_sec_type: str = "STK",
    ) -> list[Any]:
        return list(
            self.ib.reqSecDefOptParams(
                symbol.upper(),
                exchange,
                underlying_sec_type,
                underlying_con_id,
            )
        )

    def historical_daily_bars(
        self,
        symbol: str,
        duration: str = "90 D",
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[dict[str, Any]]:
        self.ib.reqMarketDataType(self.settings.market_data_type)
        contract = self.qualify_stock(symbol, exchange, currency, primary_exchange)
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=False,
        )
        rows = []
        for bar in bars:
            rows.append(
                {
                    "date": str(bar.date),
                    "open": ib_number_or_none(bar.open),
                    "high": ib_number_or_none(bar.high),
                    "low": ib_number_or_none(bar.low),
                    "close": ib_number_or_none(bar.close),
                    "volume": ib_number_or_none(bar.volume),
                }
            )
        return rows

    def snapshot_quote(
        self,
        symbol: str,
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
        timeout: int = 15,
    ) -> Quote:
        self.ib.reqMarketDataType(self.settings.market_data_type)
        contract = self.qualify_stock(symbol, exchange, currency, primary_exchange)
        ticker = self.ib.reqMktData(contract, "", True, False)

        for _ in range(max(timeout, 1)):
            self.ib.sleep(1)
            quote = Quote(
                symbol=contract.symbol,
                bid=price_or_none(ticker.bid),
                ask=price_or_none(ticker.ask),
                last=price_or_none(ticker.last),
                close=price_or_none(ticker.close),
                market_data_type=getattr(ticker, "marketDataType", None),
            )
            self._raise_blocking_market_data_errors()
            if quote.usable_price is not None:
                return quote

        raise RuntimeError(f"No usable snapshot price received for {contract.symbol}.")

    def snapshot_contract_quote(
        self,
        contract: Contract,
        timeout: int = 15,
    ) -> dict[str, Any]:
        self.ib.reqMarketDataType(self.settings.market_data_type)
        ticker = self.ib.reqMktData(contract, "", True, False)

        for _ in range(max(timeout, 1)):
            self.ib.sleep(1)
            quote = {
                "conId": getattr(contract, "conId", None),
                "symbol": getattr(contract, "symbol", ""),
                "localSymbol": getattr(contract, "localSymbol", ""),
                "secType": getattr(contract, "secType", ""),
                "bid": price_or_none(ticker.bid),
                "ask": price_or_none(ticker.ask),
                "last": price_or_none(ticker.last),
                "close": price_or_none(ticker.close),
                "marketDataType": getattr(ticker, "marketDataType", None),
            }
            self._raise_blocking_market_data_errors()
            if any(quote[field] is not None for field in ("bid", "ask", "last", "close")):
                return quote

        raise RuntimeError(
            f"No usable snapshot price received for {getattr(contract, 'localSymbol', contract)}."
        )

    def snapshot_option_quote_by_con_id(
        self,
        con_id: int,
        timeout: int = 15,
    ) -> dict[str, Any]:
        contract = self.qualify_contract_by_con_id(con_id)
        return self.snapshot_contract_quote(contract, timeout)

    def what_if_limit_order(
        self,
        account: str,
        symbol: str,
        action: str,
        quantity: float,
        limit_price: float,
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
        tif: str = "DAY",
    ) -> dict[str, Any]:
        self.require_paper_account(account)
        contract = self.qualify_stock(symbol, exchange, currency, primary_exchange)
        order = make_limit_order(account, action, quantity, limit_price, tif)
        state = self.ib.whatIfOrder(contract, order)

        if isinstance(state, list):
            if len(state) != 1:
                raise RuntimeError(f"Expected one OrderState, got {state!r}")
            state = state[0]

        return {
            "status": state.status,
            "warning": state.warningText,
            "initMarginBefore": state.initMarginBefore,
            "initMarginChange": state.initMarginChange,
            "initMarginAfter": state.initMarginAfter,
            "maintMarginChange": state.maintMarginChange,
            "equityWithLoanChange": state.equityWithLoanChange,
            "commissionCurrency": state.commissionCurrency,
            "commission": ib_number_or_none(state.commission),
            "minCommission": ib_number_or_none(state.minCommission),
            "maxCommission": ib_number_or_none(state.maxCommission),
        }

    def place_limit_order(
        self,
        account: str,
        symbol: str,
        action: str,
        quantity: float,
        limit_price: float,
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
        tif: str = "DAY",
        cancel_after: int = 0,
    ) -> dict[str, Any]:
        if not self.settings.allow_order:
            raise RuntimeError(
                "Refusing to place order. Set IB_ALLOW_ORDER=true only for paper testing."
            )

        self.require_paper_account(account)
        contract = self.qualify_stock(symbol, exchange, currency, primary_exchange)
        order = make_limit_order(account, action, quantity, limit_price, tif)
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)

        if cancel_after > 0:
            for _ in range(cancel_after):
                if not trade.isActive():
                    break
                self.ib.sleep(1)
            if trade.isActive():
                self.ib.cancelOrder(order)
                self.ib.sleep(2)

        return {
            "orderId": trade.order.orderId,
            "status": trade.orderStatus.status,
            "symbol": contract.symbol,
            "action": order.action,
            "quantity": order.totalQuantity,
            "limitPrice": order.lmtPrice,
            "account": order.account,
            "cancelled": not trade.isActive() if cancel_after > 0 else False,
        }

    def place_stock_limit_order(
        self,
        account: str,
        symbol: str,
        action: str,
        quantity: float,
        limit_price: float,
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
        tif: str = "DAY",
        order_ref: str | None = None,
    ) -> dict[str, Any]:
        contract = self.qualify_stock(symbol, exchange, currency, primary_exchange)
        return self.place_contract_limit_order(
            account=account,
            contract=contract,
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            tif=tif,
            order_ref=order_ref,
        )

    def what_if_contract_limit_order(
        self,
        account: str,
        contract: Contract,
        action: str,
        quantity: float,
        limit_price: float,
        tif: str = "DAY",
        order_ref: str | None = None,
    ) -> dict[str, Any]:
        self.require_paper_account(account)
        order = make_limit_order(account, action, quantity, limit_price, tif, order_ref)
        state = self.ib.whatIfOrder(contract, order)

        if isinstance(state, list):
            if len(state) != 1:
                raise RuntimeError(f"Expected one OrderState, got {state!r}")
            state = state[0]

        return {
            "status": state.status,
            "warning": state.warningText,
            "initMarginBefore": state.initMarginBefore,
            "initMarginChange": state.initMarginChange,
            "initMarginAfter": state.initMarginAfter,
            "maintMarginChange": state.maintMarginChange,
            "equityWithLoanChange": state.equityWithLoanChange,
            "commissionCurrency": state.commissionCurrency,
            "commission": ib_number_or_none(state.commission),
            "minCommission": ib_number_or_none(state.minCommission),
            "maxCommission": ib_number_or_none(state.maxCommission),
        }

    def place_contract_limit_order(
        self,
        account: str,
        contract: Contract,
        action: str,
        quantity: float,
        limit_price: float,
        tif: str = "DAY",
        order_ref: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.allow_order:
            raise RuntimeError(
                "Refusing to place order. Set IB_ALLOW_ORDER=true only for paper testing."
            )

        self.require_paper_account(account)
        order = make_limit_order(account, action, quantity, limit_price, tif, order_ref)
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)
        return {
            "orderId": trade.order.orderId,
            "permId": trade.order.permId,
            "status": trade.orderStatus.status,
            "conId": getattr(contract, "conId", None),
            "symbol": getattr(contract, "symbol", ""),
            "localSymbol": getattr(contract, "localSymbol", ""),
            "secType": getattr(contract, "secType", ""),
            "action": order.action,
            "quantity": order.totalQuantity,
            "limitPrice": ib_number_or_none(order.lmtPrice),
            "account": order.account,
            "orderRef": order.orderRef,
        }

    def portfolio_items(self, account: str | None = None) -> list[dict[str, Any]]:
        if account:
            self.ib.reqAccountUpdates(account)
            self.ib.sleep(1)
        rows = []
        for item in self.ib.portfolio(account or ""):
            contract = item.contract
            rows.append(
                {
                    "account": item.account,
                    "conId": getattr(contract, "conId", None),
                    "symbol": getattr(contract, "symbol", ""),
                    "localSymbol": getattr(contract, "localSymbol", ""),
                    "secType": getattr(contract, "secType", ""),
                    "right": getattr(contract, "right", ""),
                    "strike": ib_number_or_none(getattr(contract, "strike", None)),
                    "lastTradeDateOrContractMonth": getattr(
                        contract,
                        "lastTradeDateOrContractMonth",
                        "",
                    ),
                    "position": item.position,
                    "marketPrice": ib_number_or_none(item.marketPrice),
                    "marketValue": ib_number_or_none(item.marketValue),
                    "averageCost": ib_number_or_none(item.averageCost),
                    "unrealizedPNL": ib_number_or_none(item.unrealizedPNL),
                    "realizedPNL": ib_number_or_none(item.realizedPNL),
                }
            )
        return rows

    def open_orders(self) -> list[dict[str, Any]]:
        self.ib.reqAllOpenOrders()
        self.ib.sleep(1)
        rows = []
        for trade in self.ib.openTrades():
            contract = trade.contract
            order = trade.order
            status = trade.orderStatus
            rows.append(
                {
                    "orderId": order.orderId,
                    "account": order.account,
                    "symbol": contract.symbol,
                    "secType": contract.secType,
                    "action": order.action,
                    "quantity": order.totalQuantity,
                    "orderType": order.orderType,
                    "limitPrice": ib_number_or_none(getattr(order, "lmtPrice", None)),
                    "orderRef": getattr(order, "orderRef", ""),
                    "status": status.status,
                    "filled": status.filled,
                    "remaining": status.remaining,
                }
            )
        return rows

    def execution_reports(
        self,
        account: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        execution_filter = ExecutionFilter(
            acctCode=account or "",
            symbol=(symbol or "").upper(),
        )
        fills = self.ib.reqExecutions(execution_filter)
        rows = []
        for fill in fills:
            contract = fill.contract
            execution = fill.execution
            commission = fill.commissionReport
            rows.append(
                {
                    "time": fill.time.isoformat(),
                    "account": execution.acctNumber,
                    "orderId": execution.orderId,
                    "permId": execution.permId,
                    "execId": execution.execId,
                    "orderRef": execution.orderRef,
                    "symbol": getattr(contract, "symbol", ""),
                    "localSymbol": getattr(contract, "localSymbol", ""),
                    "secType": getattr(contract, "secType", ""),
                    "side": execution.side,
                    "shares": execution.shares,
                    "price": execution.price,
                    "avgPrice": execution.avgPrice,
                    "cumQty": execution.cumQty,
                    "exchange": execution.exchange,
                    "commission": ib_number_or_none(commission.commission),
                    "commissionCurrency": commission.currency,
                    "realizedPNL": ib_number_or_none(commission.realizedPNL),
                }
            )
        return rows

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        self.ib.reqAllOpenOrders()
        self.ib.sleep(1)
        for trade in self.ib.openTrades():
            if trade.order.orderId == order_id:
                self.ib.cancelOrder(trade.order)
                self.ib.sleep(2)
                return {
                    "orderId": order_id,
                    "status": trade.orderStatus.status,
                    "remaining": trade.orderStatus.remaining,
                }
        raise RuntimeError(f"Open order {order_id} was not found.")

    def _on_error(
        self,
        req_id: int,
        error_code: int,
        error_string: str,
        contract: object | None = None,
    ) -> None:
        self.errors.append((req_id, error_code, error_string))

    def _raise_blocking_market_data_errors(self) -> None:
        for _req_id, code, message in self.errors:
            if code == 10197:
                raise RuntimeError(
                    "IBKR market data is blocked by another active session: "
                    f"{message}. Close other live TWS/Gateway/Client Portal/mobile sessions."
                )


def make_limit_order(
    account: str,
    action: str,
    quantity: float,
    limit_price: float,
    tif: str = "DAY",
    order_ref: str | None = None,
) -> LimitOrder:
    normalized_action = action.upper()
    if normalized_action not in {"BUY", "SELL"}:
        raise ValueError(f"Order action must be BUY or SELL, got {action!r}")
    if quantity <= 0:
        raise ValueError("Order quantity must be positive.")
    if limit_price <= 0:
        raise ValueError("Limit price must be positive.")

    order = LimitOrder(normalized_action, quantity, limit_price)
    order.account = account
    order.tif = tif.upper()
    order.transmit = True
    if order_ref:
        order.orderRef = order_ref
    return order


def price_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def ib_number_or_none(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    if not isinstance(value, (int, float)):
        return value
    if not math.isfinite(value):
        return None
    if abs(value) >= IB_UNSET_DOUBLE * 0.99:
        return None
    return value


def sorted_rows(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get(key, "")))
