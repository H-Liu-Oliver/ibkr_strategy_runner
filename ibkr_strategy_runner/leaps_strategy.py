from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ib_async import Contract, Option

from .ibkr import IBKRClient, ib_number_or_none, price_or_none
from .live_state import (
    ManagedOptionPosition,
    ManagedOrder,
    StateStore,
    StrategyState,
    TERMINAL_ORDER_STATES,
    lifecycle_from_broker_status,
    today_iso,
)

STALE_ORDER_POLICIES = frozenset(
    {
        "leave_until_expired",
        "cancel_before_cycle",
        "replace_after_cancel",
    }
)


@dataclass(frozen=True)
class TakeProfitRule:
    max_holding_days: int
    pct_gain: float


@dataclass(frozen=True)
class LeapsStrategyConfig:
    symbol: str = "QQQ"
    primary_exchange: str | None = None
    exchange: str = "SMART"
    currency: str = "USD"
    capital_base: str = "net_liquidation"
    strategy_capital_limit: float | None = None
    buying_power_fraction: float | None = None
    risk_free_rate: float = 0.04
    hv_window: int = 30
    signal_drop: float = -0.01
    target_delta: float = 0.60
    dte_days: int = 540
    trade_fraction: float = 0.0125
    max_positions: int = 5
    dca_months: float = 14.0
    equity_allocation: float = 0.70
    option_allocation: float = 0.25
    cash_buffer_allocation: float = 0.05
    take_profit_rules: tuple[TakeProfitRule, ...] = (
        TakeProfitRule(120, 0.50),
        TakeProfitRule(180, 0.30),
        TakeProfitRule(270, 0.10),
    )
    max_holding_days: int = 270
    min_stock_order_dollars: float = 100.0
    min_option_order_dollars: float = 100.0
    history_duration: str = "90 D"
    quote_wait_seconds: int = 15
    option_exchange: str = "SMART"
    option_chain_exchange: str = ""
    option_limit_offset: float = 0.0
    stock_limit_offset: float = 0.0
    rotate_when_full: bool = True
    stale_order_policy: str = "leave_until_expired"
    max_single_order_value: float | None = None
    max_daily_order_count: int | None = None
    max_daily_notional: float | None = None
    max_total_open_order_value: float | None = None
    max_stock_position_value: float | None = None
    max_option_position_value: float | None = None
    max_option_bid_ask_spread_pct: float | None = None

    @classmethod
    def from_file(cls, path: Path) -> "LeapsStrategyConfig":
        import json

        raw = json.loads(Path(path).read_text())
        rules = tuple(
            TakeProfitRule(
                int(item["max_holding_days"]),
                float(item["pct_gain"]),
            )
            for item in raw.get("take_profit_rules", [])
        )
        payload = {key: value for key, value in raw.items() if key != "take_profit_rules"}
        if rules:
            payload["take_profit_rules"] = rules
        return cls(**payload)

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["take_profit_rules"] = [asdict(rule) for rule in self.take_profit_rules]
        return data

    def normalized_allocations(self) -> tuple[float, float, float]:
        if self.stale_order_policy not in STALE_ORDER_POLICIES:
            raise ValueError(
                "stale_order_policy must be one of: "
                f"{', '.join(sorted(STALE_ORDER_POLICIES))}."
            )
        total = self.equity_allocation + self.option_allocation + self.cash_buffer_allocation
        if total <= 0:
            raise ValueError("Allocations must sum to a positive number.")
        return (
            self.equity_allocation / total,
            self.option_allocation / total,
            self.cash_buffer_allocation / total,
        )


@dataclass(frozen=True)
class DailySignal:
    bar_date: str
    symbol: str
    close: float
    previous_close: float
    daily_return: float
    historical_volatility: float
    triggered: bool


@dataclass(frozen=True)
class OptionCandidate:
    contract: Contract
    expiry: str
    strike: float
    right: str
    multiplier: int
    theoretical_delta: float
    dte: int


@dataclass
class CycleResult:
    date: str
    mode: str
    account: str
    symbol: str
    signal: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    reason: str | None = None
    state_path: str | None = None


class LeapsTrader:
    def __init__(
        self,
        client: IBKRClient,
        config: LeapsStrategyConfig,
        state_store: StateStore,
        execute: bool = False,
    ) -> None:
        self.client = client
        self.config = config
        self.state_store = state_store
        self.execute = execute

    def run_daily_cycle(self, force: bool = False) -> CycleResult:
        account = self.client.resolve_account()
        if self.execute:
            self.client.require_trading_account(
                account,
                strategy_capital_limit=self.config.strategy_capital_limit,
                require_cap=True,
            )
        state = self.state_store.load()
        signal = self._daily_signal()
        cycle_date = signal.bar_date
        result = CycleResult(
            date=cycle_date,
            mode="execute" if self.execute else "dry-run",
            account=account,
            symbol=self.config.symbol.upper(),
            signal=asdict(signal),
            state_path=str(self.state_store.state_path),
        )

        self._reconcile_submitted_positions(state, result)
        self._apply_stale_order_policy(state, result, cycle_date)
        self._append_reconciliation_summary(state, result)
        block_reasons = reconciliation_block_reasons(result)
        if block_reasons:
            result.skipped = True
            result.reason = "reconciliation blocked trading: " + "; ".join(block_reasons[:3])
            self.state_store.save(state)
            self.state_store.record_event("cycle", asdict(result))
            return result

        completed_date = state.last_cycle_date if self.execute else state.last_dry_run_cycle_date
        if completed_date == cycle_date and not force:
            result.skipped = True
            result.reason = f"{result.mode} cycle already completed for market bar {cycle_date}"
            self.state_store.save(state)
            self.state_store.record_event("cycle", asdict(result))
            return result

        account_values = self._account_values(account)
        strategy_capital = self._strategy_capital(account_values)
        positions = self.client.positions(account)
        underlying_position = self._underlying_position(positions)
        option_value = self._managed_option_market_value(state)

        result.actions.append(
            {
                "type": "capital",
                "action": "INFO",
                "capital_base": self.config.capital_base,
                "net_liquidation": account_values.get("NetLiquidation"),
                "available_funds": account_values.get("AvailableFunds"),
                "buying_power": account_values.get("BuyingPower"),
                "strategy_capital": strategy_capital,
                "strategy_capital_limit": self.config.strategy_capital_limit,
                "buying_power_fraction": self.config.buying_power_fraction,
            }
        )
        result.actions.append(
            self._risk_status_action(
                state,
                cycle_date,
                underlying_position,
                option_value,
                signal.close,
            )
        )

        self._manage_exits(state, result)
        self._manage_dca_stock(state, result, strategy_capital, underlying_position)

        if signal.triggered:
            self._manage_option_entry(state, result, strategy_capital, option_value)
        else:
            result.actions.append(
                {
                    "type": "signal",
                    "action": "HOLD",
                    "reason": (
                        f"daily return {signal.daily_return:.4f} is above "
                        f"signal_drop {self.config.signal_drop:.4f}"
                    ),
                }
            )

        if self.execute:
            state.last_cycle_date = cycle_date
            state.dca_days_completed += 1
        else:
            state.last_dry_run_cycle_date = cycle_date
            state.dry_run_cycles_completed += 1
        self.state_store.save(state)
        self.state_store.record_event("cycle", asdict(result))
        return result

    def reconcile_state(self) -> CycleResult:
        account = self.client.resolve_account()
        state = self.state_store.load()
        result = CycleResult(
            date=today_iso(),
            mode="reconcile",
            account=account,
            symbol=self.config.symbol.upper(),
            state_path=str(self.state_store.state_path),
        )
        self._reconcile_submitted_positions(state, result)
        self._apply_stale_order_policy(state, result, result.date)
        self._append_reconciliation_summary(state, result)
        self.state_store.save(state)
        self.state_store.record_event("reconcile", asdict(result))
        return result

    def _daily_signal(self) -> DailySignal:
        bars = self.client.historical_daily_bars(
            self.config.symbol,
            duration=self.config.history_duration,
            primary_exchange=self.config.primary_exchange,
        )
        closes = [float(bar["close"]) for bar in bars if price_or_none(bar.get("close"))]
        if len(closes) < 2:
            raise RuntimeError("At least two historical closes are required for the signal.")

        returns = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes))
            if closes[index - 1] > 0
        ]
        window = returns[-max(self.config.hv_window, 1) :]
        sigma = annualized_volatility(window) or 0.20
        daily_return = returns[-1]
        return DailySignal(
            bar_date=normalize_bar_date(str(bars[-1]["date"])),
            symbol=self.config.symbol.upper(),
            close=closes[-1],
            previous_close=closes[-2],
            daily_return=daily_return,
            historical_volatility=sigma,
            triggered=daily_return <= self.config.signal_drop,
        )

    def _manage_dca_stock(
        self,
        state: StrategyState,
        result: CycleResult,
        net_liq: float,
        underlying_position: dict[str, Any] | None,
    ) -> None:
        equity_alloc, _option_alloc, _cash_alloc = self.config.normalized_allocations()
        dca_days = int(max(self.config.dca_months, 1) * 21)
        if state.dca_days_completed >= dca_days:
            result.actions.append(
                {"type": "dca", "action": "HOLD", "reason": "DCA window completed"}
            )
            return

        quote = self.client.snapshot_quote(
            self.config.symbol,
            primary_exchange=self.config.primary_exchange,
            timeout=self.config.quote_wait_seconds,
        )
        price = quote.usable_price
        if price is None:
            result.actions.append(
                {"type": "dca", "action": "HOLD", "reason": "no usable underlying quote"}
            )
            return

        current_value = underlying_position_value(underlying_position, price)
        target_value = net_liq * equity_alloc
        daily_budget = target_value / dca_days
        remaining_gap = max(target_value - current_value, 0.0)
        budget = min(daily_budget, remaining_gap)
        if budget < self.config.min_stock_order_dollars:
            result.actions.append(
                {
                    "type": "dca",
                    "action": "HOLD",
                    "reason": "budget below minimum stock order dollars",
                    "budget": budget,
                }
            )
            return

        shares = int(budget // price)
        if shares <= 0:
            result.actions.append(
                {"type": "dca", "action": "HOLD", "reason": "budget buys less than one share"}
            )
            return

        limit_price = round(price + self.config.stock_limit_offset, 2)
        order_value = shares * limit_price
        risk_block = self._risk_block_reason(
            state,
            result.date,
            "stock",
            order_value,
            current_stock_value=current_value,
        )
        if risk_block:
            result.actions.append(
                {
                    "type": "dca",
                    "action": "HOLD",
                    "reason": risk_block,
                    "order_value": order_value,
                    "quantity": shares,
                    "limit_price": limit_price,
                }
            )
            return

        action = {
            "type": "dca",
            "action": "BUY",
            "symbol": self.config.symbol.upper(),
            "quantity": shares,
            "limit_price": limit_price,
            "budget": budget,
            "order_value": order_value,
            "execute": self.execute,
        }
        if self.execute:
            order = self.client.place_stock_limit_order(
                account=state.account,
                symbol=self.config.symbol,
                action="BUY",
                quantity=shares,
                limit_price=limit_price,
                primary_exchange=self.config.primary_exchange,
                order_ref="ibkr-strategy-runner:leaps:dca",
                strategy_capital_limit=self.config.strategy_capital_limit,
            )
            action["order"] = order
            state.pending_orders.append(
                ManagedOrder(
                    type="dca",
                    order_id=order.get("orderId"),
                    perm_id=order.get("permId"),
                    symbol=self.config.symbol.upper(),
                    sec_type="STK",
                    action="BUY",
                    quantity=shares,
                    limit_price=limit_price,
                    order_value=order_value,
                    order_ref=order.get("orderRef") or "ibkr-strategy-runner:leaps:dca",
                    lifecycle_state=lifecycle_from_broker_status(order.get("status")),
                    broker_status=order.get("status"),
                    created_date=result.date,
                )
            )
        result.actions.append(action)

    def _manage_option_entry(
        self,
        state: StrategyState,
        result: CycleResult,
        net_liq: float,
        option_value: float,
    ) -> None:
        open_positions = state.open_positions()
        if len(open_positions) >= self.config.max_positions:
            if not self.config.rotate_when_full:
                result.actions.append(
                    {
                        "type": "option-entry",
                        "action": "HOLD",
                        "reason": "max option positions reached",
                    }
                )
                return
            oldest = sorted(open_positions, key=lambda pos: pos.entry_date)[0]
            self._close_position(state, oldest, "ROTATE", result)

        _equity_alloc, option_alloc, _cash_alloc = self.config.normalized_allocations()
        trade_budget = net_liq * self.config.trade_fraction
        max_option_value = net_liq * option_alloc
        if option_value + trade_budget > max_option_value:
            result.actions.append(
                {
                    "type": "option-entry",
                    "action": "HOLD",
                    "reason": "option allocation cap reached",
                    "option_value": option_value,
                    "trade_budget": trade_budget,
                    "max_option_value": max_option_value,
                }
            )
            return
        if trade_budget < self.config.min_option_order_dollars:
            result.actions.append(
                {
                    "type": "option-entry",
                    "action": "HOLD",
                    "reason": "budget below minimum option order dollars",
                    "trade_budget": trade_budget,
                }
            )
            return

        signal = result.signal or {}
        spot = float(signal["close"])
        sigma = float(signal["historical_volatility"])
        candidate = self._select_option_candidate(spot, sigma)
        quote = self.client.snapshot_contract_quote(
            candidate.contract,
            timeout=self.config.quote_wait_seconds,
        )
        limit_price = option_buy_limit(quote, self.config.option_limit_offset)
        if limit_price is None:
            result.actions.append(
                {"type": "option-entry", "action": "HOLD", "reason": "no usable option quote"}
            )
            return

        cost_per_contract = limit_price * candidate.multiplier
        contracts = int(trade_budget // cost_per_contract)
        if contracts <= 0:
            result.actions.append(
                {
                    "type": "option-entry",
                    "action": "HOLD",
                    "reason": "budget buys less than one option contract",
                    "trade_budget": trade_budget,
                    "cost_per_contract": cost_per_contract,
                }
            )
            return
        order_value = contracts * cost_per_contract
        risk_block = self._risk_block_reason(
            state,
            result.date,
            "option",
            order_value,
            option_value=option_value,
            quote=quote,
        )
        if risk_block:
            result.actions.append(
                {
                    "type": "option-entry",
                    "action": "HOLD",
                    "reason": risk_block,
                    "order_value": order_value,
                    "quantity": contracts,
                    "limit_price": limit_price,
                    "local_symbol": candidate.contract.localSymbol,
                }
            )
            return

        action = {
            "type": "option-entry",
            "action": "BUY",
            "symbol": self.config.symbol.upper(),
            "local_symbol": candidate.contract.localSymbol,
            "con_id": candidate.contract.conId,
            "expiry": candidate.expiry,
            "strike": candidate.strike,
            "right": candidate.right,
            "quantity": contracts,
            "limit_price": limit_price,
            "order_value": order_value,
            "theoretical_delta": candidate.theoretical_delta,
            "dte": candidate.dte,
            "execute": self.execute,
        }
        if self.execute:
            order = self.client.place_contract_limit_order(
                account=state.account,
                contract=candidate.contract,
                action="BUY",
                quantity=contracts,
                limit_price=limit_price,
                order_ref="ibkr-strategy-runner:leaps:entry",
                strategy_capital_limit=self.config.strategy_capital_limit,
            )
            action["order"] = order
            state.positions.append(
                ManagedOptionPosition(
                    symbol=self.config.symbol.upper(),
                    con_id=int(candidate.contract.conId),
                    local_symbol=candidate.contract.localSymbol,
                    expiry=candidate.expiry,
                    strike=float(candidate.strike),
                    right=candidate.right,
                    multiplier=int(candidate.multiplier),
                    quantity=contracts,
                    entry_date=today_iso(),
                    entry_price=limit_price,
                    order_id=order.get("orderId"),
                    status="SUBMITTED",
                )
            )
            state.pending_orders.append(
                ManagedOrder(
                    type="option-entry",
                    order_id=order.get("orderId"),
                    perm_id=order.get("permId"),
                    symbol=self.config.symbol.upper(),
                    local_symbol=candidate.contract.localSymbol,
                    sec_type="OPT",
                    action="BUY",
                    quantity=contracts,
                    limit_price=limit_price,
                    multiplier=candidate.multiplier,
                    order_value=order_value,
                    order_ref=order.get("orderRef") or "ibkr-strategy-runner:leaps:entry",
                    lifecycle_state=lifecycle_from_broker_status(order.get("status")),
                    broker_status=order.get("status"),
                    created_date=result.date,
                )
            )
        result.actions.append(action)

    def _risk_block_reason(
        self,
        state: StrategyState,
        cycle_date: str,
        order_kind: str,
        order_value: float,
        current_stock_value: float = 0.0,
        option_value: float = 0.0,
        quote: dict[str, Any] | None = None,
    ) -> str | None:
        if self.config.max_single_order_value is not None:
            if order_value > self.config.max_single_order_value:
                return (
                    f"risk limit max_single_order_value exceeded: "
                    f"{order_value:.2f} > {self.config.max_single_order_value:.2f}"
                )

        usage = daily_order_usage(state, cycle_date)
        if self.config.max_daily_order_count is not None:
            if usage["count"] + 1 > self.config.max_daily_order_count:
                return (
                    f"risk limit max_daily_order_count exceeded: "
                    f"{usage['count'] + 1} > {self.config.max_daily_order_count}"
                )

        if self.config.max_daily_notional is not None:
            next_daily_notional = usage["notional"] + order_value
            if next_daily_notional > self.config.max_daily_notional:
                return (
                    f"risk limit max_daily_notional exceeded: "
                    f"{next_daily_notional:.2f} > {self.config.max_daily_notional:.2f}"
                )

        if self.config.max_total_open_order_value is not None:
            next_open_value = total_open_order_value(state) + order_value
            if next_open_value > self.config.max_total_open_order_value:
                return (
                    f"risk limit max_total_open_order_value exceeded: "
                    f"{next_open_value:.2f} > {self.config.max_total_open_order_value:.2f}"
                )

        if order_kind == "stock" and self.config.max_stock_position_value is not None:
            next_stock_value = current_stock_value + order_value
            if next_stock_value > self.config.max_stock_position_value:
                return (
                    f"risk limit max_stock_position_value exceeded: "
                    f"{next_stock_value:.2f} > {self.config.max_stock_position_value:.2f}"
                )

        if order_kind == "option" and self.config.max_option_position_value is not None:
            next_option_value = option_value + order_value
            if next_option_value > self.config.max_option_position_value:
                return (
                    f"risk limit max_option_position_value exceeded: "
                    f"{next_option_value:.2f} > {self.config.max_option_position_value:.2f}"
                )

        if order_kind == "option" and self.config.max_option_bid_ask_spread_pct is not None:
            spread_pct = bid_ask_spread_pct(quote or {})
            if spread_pct is None:
                return "risk limit max_option_bid_ask_spread_pct could not be checked"
            if spread_pct > self.config.max_option_bid_ask_spread_pct:
                return (
                    f"risk limit max_option_bid_ask_spread_pct exceeded: "
                    f"{spread_pct:.4f} > {self.config.max_option_bid_ask_spread_pct:.4f}"
                )

        return None

    def _risk_status_action(
        self,
        state: StrategyState,
        cycle_date: str,
        underlying_position: dict[str, Any] | None,
        option_value: float,
        underlying_price: float,
    ) -> dict[str, Any]:
        daily_usage = daily_order_usage(state, cycle_date)
        return {
            "type": "risk",
            "action": "INFO",
            "daily_order_count": daily_usage["count"],
            "daily_notional": daily_usage["notional"],
            "total_open_order_value": total_open_order_value(state),
            "stock_position_value": underlying_position_value(
                underlying_position,
                underlying_price,
            ),
            "option_position_value": option_value,
            "limits": {
                "max_single_order_value": self.config.max_single_order_value,
                "max_daily_order_count": self.config.max_daily_order_count,
                "max_daily_notional": self.config.max_daily_notional,
                "max_total_open_order_value": self.config.max_total_open_order_value,
                "max_stock_position_value": self.config.max_stock_position_value,
                "max_option_position_value": self.config.max_option_position_value,
                "max_option_bid_ask_spread_pct": self.config.max_option_bid_ask_spread_pct,
            },
        }

    def _manage_exits(self, state: StrategyState, result: CycleResult) -> None:
        for pos in list(state.open_positions()):
            quote = self.client.snapshot_option_quote_by_con_id(
                pos.con_id,
                timeout=self.config.quote_wait_seconds,
            )
            exit_price = option_sell_limit(quote, self.config.option_limit_offset)
            if exit_price is None:
                result.actions.append(
                    {
                        "type": "option-exit",
                        "action": "HOLD",
                        "local_symbol": pos.local_symbol,
                        "reason": "no usable option quote",
                    }
                )
                continue
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0
            holding_days = days_since(pos.entry_date)

            reason = None
            for rule in self.config.take_profit_rules:
                if holding_days <= rule.max_holding_days and pnl_pct >= rule.pct_gain:
                    reason = "TP"
                    break
            if reason is None and holding_days > self.config.max_holding_days:
                reason = "FORCE"

            if reason:
                self._close_position(state, pos, reason, result, exit_price=exit_price)
            else:
                result.actions.append(
                    {
                        "type": "option-exit",
                        "action": "HOLD",
                        "local_symbol": pos.local_symbol,
                        "pnl_pct": pnl_pct,
                        "holding_days": holding_days,
                    }
                )

    def _close_position(
        self,
        state: StrategyState,
        pos: ManagedOptionPosition,
        reason: str,
        result: CycleResult,
        exit_price: float | None = None,
    ) -> None:
        contract = self.client.qualify_contract_by_con_id(pos.con_id)
        if exit_price is None:
            quote = self.client.snapshot_contract_quote(
                contract,
                timeout=self.config.quote_wait_seconds,
            )
            exit_price = option_sell_limit(quote, self.config.option_limit_offset)
        action = {
            "type": "option-exit",
            "action": "SELL",
            "local_symbol": pos.local_symbol,
            "con_id": pos.con_id,
            "quantity": pos.quantity,
            "limit_price": exit_price,
            "reason": reason,
            "execute": self.execute,
        }
        if exit_price is None:
            action["action"] = "HOLD"
            action["reason"] = "no usable option quote"
            result.actions.append(action)
            return

        if self.execute:
            order = self.client.place_contract_limit_order(
                account=state.account,
                contract=contract,
                action="SELL",
                quantity=pos.quantity,
                limit_price=exit_price,
                order_ref=f"ibkr-strategy-runner:leaps:exit:{reason.lower()}",
                strategy_capital_limit=self.config.strategy_capital_limit,
            )
            action["order"] = order
            pos.status = "CLOSE_SUBMITTED"
            pos.close_order_id = order.get("orderId")
            pos.close_price = exit_price
            pos.close_reason = reason
            state.pending_orders.append(
                ManagedOrder(
                    type="option-exit",
                    order_id=order.get("orderId"),
                    perm_id=order.get("permId"),
                    symbol=self.config.symbol.upper(),
                    local_symbol=pos.local_symbol,
                    sec_type="OPT",
                    action="SELL",
                    quantity=pos.quantity,
                    limit_price=exit_price,
                    multiplier=pos.multiplier,
                    order_value=abs(pos.quantity * exit_price * pos.multiplier),
                    order_ref=(
                        order.get("orderRef")
                        or f"ibkr-strategy-runner:leaps:exit:{reason.lower()}"
                    ),
                    lifecycle_state=lifecycle_from_broker_status(order.get("status")),
                    broker_status=order.get("status"),
                    created_date=result.date,
                )
            )
        result.actions.append(action)

    def _select_option_candidate(self, spot: float, sigma: float) -> OptionCandidate:
        underlying = self.client.qualify_stock(
            self.config.symbol,
            exchange=self.config.exchange,
            currency=self.config.currency,
            primary_exchange=self.config.primary_exchange,
        )
        chains = self.client.option_chains(
            self.config.symbol,
            int(underlying.conId),
            exchange=self.config.option_chain_exchange,
        )
        if not chains:
            raise RuntimeError(f"No option chains returned for {self.config.symbol}.")

        chain = choose_chain(chains, self.config.option_exchange)
        target_expiry = choose_expiry(chain.expirations, self.config.dte_days)
        dte = max((parse_yyyymmdd(target_expiry) - date.today()).days, 1)
        t = dte / 365.0
        candidate_strikes = [
            float(strike)
            for strike in chain.strikes
            if strike > 0 and 0.35 * spot <= float(strike) <= 1.75 * spot
        ]
        if not candidate_strikes:
            raise RuntimeError(f"No usable strikes returned for {self.config.symbol}.")

        selected_strike = min(
            candidate_strikes,
            key=lambda strike: abs(
                call_delta(spot, strike, t, sigma, self.config.risk_free_rate)
                - self.config.target_delta
            ),
        )
        delta = call_delta(spot, selected_strike, t, sigma, self.config.risk_free_rate)
        multiplier = int(chain.multiplier or "100")
        contract = Option(
            self.config.symbol.upper(),
            target_expiry,
            selected_strike,
            "C",
            self.config.option_exchange,
            multiplier=str(multiplier),
            currency=self.config.currency,
            tradingClass=chain.tradingClass,
        )
        [qualified] = self.client.ib.qualifyContracts(contract)
        return OptionCandidate(
            contract=qualified,
            expiry=target_expiry,
            strike=selected_strike,
            right="C",
            multiplier=multiplier,
            theoretical_delta=delta,
            dte=dte,
        )

    def _reconcile_submitted_positions(
        self,
        state: StrategyState,
        result: CycleResult,
    ) -> None:
        positions = self.client.positions(state.account)
        con_ids = {
            int(item["conId"]): float(item["position"])
            for item in positions
            if item.get("conId") is not None
        }
        open_orders = self.client.open_orders()
        executions = self.client.execution_reports(state.account, self.config.symbol)
        executions_by_order_id: dict[int, list[dict[str, Any]]] = {}
        for execution in executions:
            order_id = execution.get("orderId")
            if order_id is None:
                continue
            executions_by_order_id.setdefault(int(order_id), []).append(execution)
        open_order_ids = {
            int(order["orderId"])
            for order in open_orders
            if order.get("orderId") is not None
        }
        open_orders_by_id = {
            int(order["orderId"]): order
            for order in open_orders
            if order.get("orderId") is not None
        }

        active_pending_orders: list[ManagedOrder] = []
        known_pending_order_ids = {
            int(order.order_id)
            for order in state.pending_orders
            if order.order_id is not None
        }
        for pending_order in state.pending_orders:
            order_id = pending_order.order_id
            if order_id is not None and int(order_id) in open_orders_by_id:
                latest = open_orders_by_id[int(order_id)]
                pending_order.sync_from_broker(latest)
                if pending_order.lifecycle_state in TERMINAL_ORDER_STATES:
                    fills = executions_by_order_id.get(int(order_id), [])
                    pending_order.mark_cleared(fills, today_iso())
                    state.completed_orders.append(pending_order)
                    result.actions.append(
                        {
                            "type": "reconcile",
                            "action": "ORDER_TERMINAL",
                            "order_id": order_id,
                            "symbol": pending_order.symbol,
                            "broker_status": pending_order.broker_status,
                            "lifecycle_state": pending_order.lifecycle_state,
                            "fills": fills,
                            "reason": "IBKR returned a terminal order status",
                        }
                    )
                else:
                    active_pending_orders.append(pending_order)
                if pending_order.lifecycle_state == "partially_filled":
                    result.actions.append(
                        {
                            "type": "reconcile",
                            "action": "ORDER_PARTIALLY_FILLED",
                            "order_id": order_id,
                            "symbol": pending_order.symbol,
                            "filled": pending_order.filled,
                            "remaining": pending_order.remaining,
                            "reason": "order remains open with a partial fill",
                        }
                    )
                elif pending_order.lifecycle_state == "unknown":
                    result.actions.append(
                        {
                            "type": "reconcile",
                            "action": "CHECK_ORDER_STATUS",
                            "order_id": order_id,
                            "symbol": pending_order.symbol,
                            "broker_status": pending_order.broker_status,
                            "reason": "IBKR returned an unknown order status for an open bot order",
                            "blocking": True,
                        }
                    )
            else:
                fills = executions_by_order_id.get(int(order_id), []) if order_id is not None else []
                pending_order.mark_cleared(fills, today_iso())
                state.completed_orders.append(pending_order)
                result.actions.append(
                    {
                        "type": "reconcile",
                        "action": "PENDING_ORDER_CLEARED",
                        "order_id": order_id,
                        "symbol": pending_order.symbol,
                        "lifecycle_state": pending_order.lifecycle_state,
                        "fills": fills,
                        "reason": (
                            "order is no longer open at IBKR; fills were found"
                            if fills
                            else (
                                "order is no longer open at IBKR; no fill report was "
                                "returned; check manually before placing replacement orders"
                            )
                        ),
                        "blocking": not fills,
                    }
                )
        state.pending_orders = active_pending_orders

        for order_id, open_order in open_orders_by_id.items():
            order_ref = str(open_order.get("orderRef") or "")
            if order_id in known_pending_order_ids:
                continue
            if not order_ref.startswith("ibkr-strategy-runner:leaps:"):
                continue
            adopted_order = ManagedOrder.from_broker_open_order(
                open_order,
                order_type="dca" if order_ref.endswith(":dca") else "managed",
            )
            state.pending_orders.append(adopted_order)
            result.actions.append(
                {
                    "type": "reconcile",
                    "action": "ADOPT_OPEN_ORDER",
                    "order_id": order_id,
                    "symbol": open_order.get("symbol"),
                    "order_ref": order_ref,
                    "lifecycle_state": adopted_order.lifecycle_state,
                    "blocking": True,
                    "reason": (
                        "open bot order existed at IBKR but was not present in local state; "
                        "review before placing new orders"
                    ),
                }
            )

        for pos in state.submitted_positions():
            if con_ids.get(pos.con_id, 0.0) > 0:
                pos.status = "OPEN"
                result.actions.append(
                    {
                        "type": "reconcile",
                        "action": "MARK_OPEN",
                        "local_symbol": pos.local_symbol,
                        "con_id": pos.con_id,
                    }
                )
            elif pos.order_id not in open_order_ids:
                result.actions.append(
                    {
                        "type": "reconcile",
                        "action": "CHECK_MANUALLY",
                        "local_symbol": pos.local_symbol,
                        "order_id": pos.order_id,
                        "reason": "submitted order is not open and position was not found",
                        "blocking": True,
                    }
                )

        for pos in state.positions:
            if pos.status == "CLOSE_SUBMITTED" and pos.close_order_id not in open_order_ids:
                if con_ids.get(pos.con_id, 0.0) <= 0:
                    pos.status = "CLOSED"
                    pos.closed_date = today_iso()
                    result.actions.append(
                        {
                            "type": "reconcile",
                            "action": "MARK_CLOSED",
                            "local_symbol": pos.local_symbol,
                            "con_id": pos.con_id,
                        }
                    )
                else:
                    result.actions.append(
                        {
                            "type": "reconcile",
                            "action": "CHECK_MANUALLY",
                            "local_symbol": pos.local_symbol,
                            "order_id": pos.close_order_id,
                            "reason": "close order is not open but position is still present",
                            "blocking": True,
                        }
                    )

        for order in state.completed_orders:
            if order.lifecycle_state != "unknown":
                continue
            result.actions.append(
                {
                    "type": "reconcile",
                    "action": "CHECK_UNKNOWN_COMPLETED_ORDER",
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "reason": (
                        "completed bot order has unknown lifecycle state; resolve it "
                        "before placing new orders"
                    ),
                    "blocking": True,
                }
            )

    def _append_reconciliation_summary(
        self,
        state: StrategyState,
        result: CycleResult,
    ) -> None:
        block_reasons = reconciliation_block_reasons(result)
        result.actions.append(
            {
                "type": "reconcile",
                "action": "SUMMARY",
                "pending_orders": len(state.pending_orders),
                "completed_orders": len(state.completed_orders),
                "open_positions": len(state.open_positions()),
                "blocking": bool(block_reasons),
                "block_reasons": block_reasons,
            }
        )

    def _apply_stale_order_policy(
        self,
        state: StrategyState,
        result: CycleResult,
        cycle_date: str,
    ) -> None:
        if self.config.stale_order_policy not in STALE_ORDER_POLICIES:
            raise ValueError(
                "stale_order_policy must be one of: "
                f"{', '.join(sorted(STALE_ORDER_POLICIES))}."
            )

        active_orders: list[ManagedOrder] = []
        for order in state.pending_orders:
            if not is_stale_order(order, cycle_date):
                active_orders.append(order)
                continue

            action: dict[str, Any] = {
                "type": "reconcile",
                "action": "STALE_ORDER",
                "order_id": order.order_id,
                "symbol": order.symbol,
                "created_date": order.created_date,
                "cycle_date": cycle_date,
                "policy": self.config.stale_order_policy,
            }

            if self.config.stale_order_policy == "leave_until_expired":
                action.update(
                    {
                        "reason": (
                            "stale order is still open; leaving it for IBKR to "
                            "expire before placing replacement orders"
                        ),
                        "blocking": True,
                    }
                )
                result.actions.append(action)
                active_orders.append(order)
                continue

            if not self.execute:
                action.update(
                    {
                        "execute": False,
                        "reason": (
                            "stale order would be cancelled in execute mode; "
                            "dry-run leaves it unchanged"
                        ),
                        "blocking": True,
                    }
                )
                result.actions.append(action)
                active_orders.append(order)
                continue

            if order.order_id is None:
                action.update(
                    {
                        "execute": True,
                        "reason": "stale order has no broker order id and cannot be cancelled",
                        "blocking": True,
                    }
                )
                result.actions.append(action)
                active_orders.append(order)
                continue

            cancel_result = self.client.cancel_order(order.order_id)
            order.sync_from_broker(
                {
                    "orderId": order.order_id,
                    "status": cancel_result.get("status"),
                    "remaining": cancel_result.get("remaining"),
                    "filled": order.filled,
                }
            )
            action["execute"] = True
            action["cancel"] = cancel_result

            if order.lifecycle_state not in TERMINAL_ORDER_STATES:
                action.update(
                    {
                        "reason": "stale order cancellation is not terminal yet",
                        "lifecycle_state": order.lifecycle_state,
                        "blocking": True,
                    }
                )
                result.actions.append(action)
                active_orders.append(order)
                continue

            order.mark_cleared([], today_iso())
            state.completed_orders.append(order)
            action["lifecycle_state"] = order.lifecycle_state
            if self.config.stale_order_policy == "cancel_before_cycle":
                action.update(
                    {
                        "reason": (
                            "stale order was cancelled; wait until the next cycle "
                            "before placing a replacement"
                        ),
                        "blocking": True,
                    }
                )
            else:
                action.update(
                    {
                        "reason": "stale order was cancelled; same-cycle replacement is enabled",
                        "blocking": False,
                    }
                )
            result.actions.append(action)

        state.pending_orders = active_orders

    def _account_values(self, account: str) -> dict[str, float]:
        values: dict[str, float] = {}
        for row in self.client.account_summary(account):
            try:
                values[row["tag"]] = float(row["value"])
            except (TypeError, ValueError):
                continue
        if "NetLiquidation" not in values:
            raise RuntimeError("NetLiquidation was not available from account summary.")
        return values

    def _strategy_capital(self, account_values: dict[str, float]) -> float:
        if self.config.capital_base == "buying_power_fraction":
            fraction = self.config.buying_power_fraction
            if fraction is None:
                raise ValueError(
                    "buying_power_fraction must be set when capital_base is buying_power_fraction."
                )
            if not 0 < fraction <= 1:
                raise ValueError("buying_power_fraction must be greater than 0 and at most 1.")
            base = account_values.get("BuyingPower")
            if base is None:
                raise RuntimeError("BuyingPower was not available from account summary.")
            strategy_capital = base * fraction
        elif self.config.capital_base == "available_funds":
            base = account_values.get("AvailableFunds")
            if base is None:
                raise RuntimeError("AvailableFunds was not available from account summary.")
            strategy_capital = base
        elif self.config.capital_base == "net_liquidation":
            strategy_capital = account_values["NetLiquidation"]
        else:
            raise ValueError(
                "capital_base must be one of: net_liquidation, available_funds, "
                "buying_power_fraction."
            )

        if self.config.strategy_capital_limit is not None:
            if self.config.strategy_capital_limit <= 0:
                raise ValueError("strategy_capital_limit must be positive when set.")
            strategy_capital = min(strategy_capital, self.config.strategy_capital_limit)
        return strategy_capital
        raise RuntimeError("NetLiquidation was not available from account summary.")

    def _underlying_position(self, portfolio: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in portfolio:
            if item.get("symbol") == self.config.symbol.upper() and item.get("secType") == "STK":
                return item
        return None

    def _managed_option_market_value(
        self,
        state: StrategyState,
    ) -> float:
        total = 0.0
        for pos in state.open_positions():
            quote = self.client.snapshot_option_quote_by_con_id(
                pos.con_id,
                timeout=self.config.quote_wait_seconds,
            )
            price = option_sell_limit(quote, self.config.option_limit_offset)
            if price is not None:
                total += abs(price * pos.quantity * pos.multiplier)
        return total


def choose_chain(chains: list[Any], preferred_exchange: str) -> Any:
    if preferred_exchange:
        for chain in chains:
            if chain.exchange.upper() == preferred_exchange.upper():
                return chain
    return max(chains, key=lambda chain: len(chain.expirations) * len(chain.strikes))


def choose_expiry(expirations: list[str], target_dte: int) -> str:
    if not expirations:
        raise RuntimeError("Option chain did not include expirations.")
    today = date.today()
    return min(
        expirations,
        key=lambda expiry: abs((parse_yyyymmdd(expiry) - today).days - target_dte),
    )


def parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def normalize_bar_date(value: str) -> str:
    text = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return today_iso()


def days_since(value: str) -> int:
    return (date.today() - date.fromisoformat(value)).days


def annualized_volatility(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / len(returns)
    return math.sqrt(variance) * math.sqrt(252)


def call_delta(spot: float, strike: float, t: float, sigma: float, risk_free_rate: float) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    if t <= 0 or sigma <= 0:
        return 1.0 if spot > strike else 0.0
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate + 0.5 * sigma**2) * t
    ) / (sigma * math.sqrt(t))
    return normal_cdf(d1)


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def option_buy_limit(quote: dict[str, Any], offset: float) -> float | None:
    bid = price_or_none(quote.get("bid"))
    ask = price_or_none(quote.get("ask"))
    last = price_or_none(quote.get("last"))
    close = price_or_none(quote.get("close"))
    if bid is not None and ask is not None and ask >= bid:
        return round(((bid + ask) / 2.0) + offset, 2)
    for price in (ask, last, close, bid):
        if price is not None:
            return round(price + offset, 2)
    return None


def option_sell_limit(quote: dict[str, Any], offset: float) -> float | None:
    bid = price_or_none(quote.get("bid"))
    ask = price_or_none(quote.get("ask"))
    last = price_or_none(quote.get("last"))
    close = price_or_none(quote.get("close"))
    if bid is not None and ask is not None and ask >= bid:
        return round(max(((bid + ask) / 2.0) - offset, 0.01), 2)
    for price in (bid, last, close, ask):
        if price is not None:
            return round(max(price - offset, 0.01), 2)
    return None


def daily_order_usage(state: StrategyState, cycle_date: str) -> dict[str, float]:
    count = 0
    notional = 0.0
    for order in [*state.pending_orders, *state.completed_orders]:
        created_date = (
            order.created_date
            if isinstance(order, ManagedOrder)
            else order.get("createdDate")
        )
        if created_date != cycle_date:
            continue
        count += 1
        notional += pending_order_value(order)
    return {"count": count, "notional": notional}


def total_open_order_value(state: StrategyState) -> float:
    return sum(pending_order_value(order) for order in state.pending_orders)


def is_stale_order(order: ManagedOrder, cycle_date: str) -> bool:
    if order.lifecycle_state in TERMINAL_ORDER_STATES:
        return False
    if order.lifecycle_state == "unknown":
        return False
    if not order.created_date:
        return False
    return order.created_date < cycle_date


def reconciliation_block_reasons(result: CycleResult) -> list[str]:
    reasons = []
    for action in result.actions:
        if action.get("action") == "SUMMARY":
            continue
        if not action.get("blocking"):
            continue
        reason = action.get("reason")
        reasons.append(str(reason or action.get("action") or "reconciliation blocked trading"))
    return reasons


def underlying_position_value(
    underlying_position: dict[str, Any] | None,
    fallback_price: float,
) -> float:
    if not underlying_position:
        return 0.0
    if underlying_position.get("marketValue") is not None:
        return float(underlying_position["marketValue"])
    return float(underlying_position.get("position") or 0.0) * fallback_price


def pending_order_value(order: ManagedOrder | dict[str, Any]) -> float:
    if isinstance(order, ManagedOrder):
        if order.order_value is not None:
            return float(order.order_value)
        quantity = float(order.quantity or 0.0)
        limit_price = float(order.limit_price or 0.0)
        multiplier = float(
            order.multiplier
            or (100.0 if order.sec_type.upper() == "OPT" else 1.0)
        )
        return abs(quantity * limit_price * multiplier)

    if order.get("orderValue") is not None:
        return float(order["orderValue"])
    quantity = float(order.get("quantity") or 0.0)
    limit_price = float(order.get("limitPrice") or 0.0)
    multiplier = float(
        order.get("multiplier")
        or (100.0 if str(order.get("secType")).upper() == "OPT" else 1.0)
    )
    return abs(quantity * limit_price * multiplier)


def bid_ask_spread_pct(quote: dict[str, Any]) -> float | None:
    bid = price_or_none(quote.get("bid"))
    ask = price_or_none(quote.get("ask"))
    if bid is None or ask is None or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid
