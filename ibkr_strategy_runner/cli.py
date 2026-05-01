from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .config import Settings, settings_from_args
from .ibkr import IBKRClient, sorted_rows
from .leaps_strategy import LeapsStrategyConfig, LeapsTrader
from .live_state import StateStore
from .models import Quote, ThresholdDecision
from .strategy import evaluate_threshold


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "handler"):
        parser.print_help()
        return

    try:
        settings = settings_from_args(args)
        result = args.handler(settings, args)
        if result is not None:
            emit(result, json_output=args.json)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        print(f"error: {message}", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc()
        raise SystemExit(1) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ibkr-strategy-runner",
        description="IBKR paper-trading strategy runner.",
    )
    parser.add_argument("--env-file", help="Path to dotenv file.")
    parser.add_argument("--host", help="IBKR host. Defaults to IB_HOST or 127.0.0.1.")
    parser.add_argument("--port", type=int, help="IBKR port. Defaults to IB_PORT or 4002.")
    parser.add_argument(
        "--client-id",
        type=int,
        help="IBKR client id. Defaults to IB_CLIENT_ID or 201.",
    )
    parser.add_argument("--account", help="IBKR account id. Defaults to IB_ACCOUNT or first account.")
    parser.add_argument("--exchange", help="Default exchange. Defaults to SMART.")
    parser.add_argument("--currency", help="Default currency. Defaults to USD.")
    parser.add_argument("--timeout", type=float, help="IBKR connection timeout in seconds.")
    parser.add_argument(
        "--request-timeout",
        type=float,
        help="IBKR request timeout in seconds.",
    )
    parser.add_argument(
        "--market-data-type",
        type=int,
        default=None,
        choices=(1, 2, 3, 4),
        help="1 live, 2 frozen, 3 delayed, 4 delayed frozen. Defaults to 3.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--debug", action="store_true", help="Print full exception tracebacks.")

    subparsers = parser.add_subparsers(dest="command")

    connect = subparsers.add_parser("connect", help="Verify IBKR connectivity.")
    connect.set_defaults(handler=cmd_connect)

    account = subparsers.add_parser("account", help="Show account summary.")
    account.set_defaults(handler=cmd_account)

    positions = subparsers.add_parser("positions", help="Show current positions.")
    positions.set_defaults(handler=cmd_positions)

    quote = subparsers.add_parser("quote", help="Request a delayed snapshot quote.")
    add_symbol_args(quote)
    quote.add_argument("--wait", type=int, default=15, help="Seconds to wait for usable data.")
    quote.set_defaults(handler=cmd_quote)

    what_if = subparsers.add_parser("what-if", help="Run an IBKR what-if limit order.")
    add_order_args(what_if)
    what_if.set_defaults(handler=cmd_what_if)

    order = subparsers.add_parser("order", help="Place a guarded paper limit order.")
    add_order_args(order)
    order.add_argument(
        "--cancel-after",
        type=int,
        default=0,
        help="Cancel the order if still active after this many seconds.",
    )
    order.set_defaults(handler=cmd_order)

    open_orders = subparsers.add_parser("open-orders", help="Show open orders.")
    open_orders.set_defaults(handler=cmd_open_orders)

    cancel = subparsers.add_parser("cancel", help="Cancel an open order by order id.")
    cancel.add_argument("--order-id", type=int, required=True)
    cancel.set_defaults(handler=cmd_cancel)

    auto = subparsers.add_parser(
        "auto-threshold",
        help="Run a simple threshold strategy. Dry-run unless --execute is set.",
    )
    add_symbol_args(auto)
    auto.add_argument("--qty", type=float, required=True)
    auto.add_argument("--buy-below", type=float)
    auto.add_argument("--sell-above", type=float)
    auto.add_argument(
        "--limit-offset",
        type=float,
        default=0.0,
        help="Offset added to the observed price when creating a limit order.",
    )
    auto.add_argument("--iterations", type=int, default=1)
    auto.add_argument("--interval", type=float, default=30.0)
    auto.add_argument("--execute", action="store_true", help="Submit paper orders when triggered.")
    auto.add_argument(
        "--cancel-after",
        type=int,
        default=0,
        help="Cancel submitted orders if still active after this many seconds.",
    )
    auto.add_argument("--wait", type=int, default=15, help="Seconds to wait for each quote.")
    auto.set_defaults(handler=cmd_auto_threshold)

    leaps_once = subparsers.add_parser(
        "leaps-once",
        help="Run one daily LEAPS-overlay cycle with persistent state.",
    )
    add_leaps_args(leaps_once)
    leaps_once.add_argument(
        "--force",
        action="store_true",
        help="Run even if today's cycle is already marked complete.",
    )
    leaps_once.set_defaults(handler=cmd_leaps_once)

    run_leaps = subparsers.add_parser(
        "run-leaps",
        help="Run the LEAPS-overlay daemon loop. Use systemd for restart supervision.",
    )
    add_leaps_args(run_leaps)
    run_leaps.add_argument(
        "--interval",
        type=float,
        default=3600.0,
        help="Seconds between cycle attempts. Defaults to 3600.",
    )
    run_leaps.set_defaults(handler=cmd_run_leaps)

    leaps_state = subparsers.add_parser(
        "leaps-state",
        help="Inspect persisted LEAPS-overlay state.",
    )
    add_leaps_state_args(leaps_state)
    leaps_state.set_defaults(handler=cmd_leaps_state)

    reconcile = subparsers.add_parser(
        "leaps-reconcile",
        help="Reconcile persisted LEAPS state with IBKR positions, open orders, and executions.",
    )
    add_leaps_state_args(reconcile)
    reconcile.set_defaults(handler=cmd_leaps_reconcile)

    example = subparsers.add_parser(
        "leaps-example-config",
        help="Print an example LEAPS-overlay config JSON.",
    )
    example.set_defaults(handler=cmd_leaps_example_config)

    unit = subparsers.add_parser(
        "systemd-unit",
        help="Print a user systemd service unit for run-leaps.",
    )
    add_leaps_state_args(unit)
    unit.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable for ExecStart.",
    )
    unit.add_argument(
        "--working-directory",
        default=str(Path.cwd()),
        help="WorkingDirectory for the service.",
    )
    unit.add_argument("--execute", action="store_true", help="Include --execute in ExecStart.")
    unit.set_defaults(handler=cmd_systemd_unit)

    return parser


def add_symbol_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("symbol", help="Stock symbol, for example AAPL.")
    parser.add_argument("--primary-exchange", help="Primary exchange, for example NASDAQ.")


def add_order_args(parser: argparse.ArgumentParser) -> None:
    add_symbol_args(parser)
    parser.add_argument("--action", required=True, choices=("BUY", "SELL"))
    parser.add_argument("--qty", type=float, required=True)
    parser.add_argument("--limit", type=float, required=True, dest="limit_price")
    parser.add_argument("--tif", default="DAY", help="Time in force. Defaults to DAY.")


def add_leaps_state_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="LEAPS strategy config JSON.")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.getenv("IBKR_STRATEGY_RUNNER_STATE_DIR", "~/.local/state/ibkr-strategy-runner")).expanduser(),
        help="Directory for durable strategy state and journal files.",
    )


def add_leaps_args(parser: argparse.ArgumentParser) -> None:
    add_leaps_state_args(parser)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit paper orders. Also requires IB_ALLOW_ORDER=true.",
    )


def cmd_connect(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    with IBKRClient(settings) as client:
        return {
            "connected": client.ib.isConnected(),
            "serverVersion": client.ib.client.serverVersion(),
            "managedAccounts": client.managed_accounts(),
            "host": settings.host,
            "port": settings.port,
            "clientId": settings.client_id,
        }


def cmd_account(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    with IBKRClient(settings) as client:
        account = client.resolve_account()
        return {"account": account, "summary": client.account_summary(account)}


def cmd_positions(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    with IBKRClient(settings) as client:
        account = settings.account
        return {"positions": sorted_rows(client.positions(account), "symbol")}


def cmd_quote(settings: Settings, args: argparse.Namespace) -> Quote:
    with IBKRClient(settings) as client:
        return client.snapshot_quote(
            args.symbol,
            primary_exchange=args.primary_exchange,
            timeout=args.wait,
        )


def cmd_what_if(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    with IBKRClient(settings) as client:
        account = client.resolve_account()
        result = client.what_if_limit_order(
            account,
            args.symbol,
            args.action,
            args.qty,
            args.limit_price,
            primary_exchange=args.primary_exchange,
            tif=args.tif,
        )
        return {"account": account, "symbol": args.symbol.upper(), "whatIf": result}


def cmd_order(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    with IBKRClient(settings) as client:
        account = client.resolve_account()
        result = client.place_limit_order(
            account,
            args.symbol,
            args.action,
            args.qty,
            args.limit_price,
            primary_exchange=args.primary_exchange,
            tif=args.tif,
            cancel_after=args.cancel_after,
        )
        return {"account": account, "order": result}


def cmd_open_orders(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    with IBKRClient(settings) as client:
        return {"openOrders": sorted_rows(client.open_orders(), "orderId")}


def cmd_cancel(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    with IBKRClient(settings) as client:
        return {"cancel": client.cancel_order(args.order_id)}


def cmd_auto_threshold(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    if args.buy_below is None and args.sell_above is None:
        raise ValueError("Set at least one of --buy-below or --sell-above.")
    if args.iterations <= 0:
        raise ValueError("--iterations must be positive.")

    decisions: list[dict[str, Any]] = []
    with IBKRClient(settings) as client:
        account = client.resolve_account()
        for index in range(args.iterations):
            quote = client.snapshot_quote(
                args.symbol,
                primary_exchange=args.primary_exchange,
                timeout=args.wait,
            )
            decision = evaluate_threshold(
                args.symbol,
                quote,
                args.qty,
                args.buy_below,
                args.sell_above,
                args.limit_offset,
            )
            row: dict[str, Any] = {
                "iteration": index + 1,
                "quote": to_plain_data(quote),
                "decision": to_plain_data(decision),
                "executed": False,
                "whatIf": None,
                "order": None,
            }

            if decision.action != "HOLD" and decision.limit_price is not None:
                row["whatIf"] = client.what_if_limit_order(
                    account,
                    args.symbol,
                    decision.action,
                    decision.quantity,
                    decision.limit_price,
                    primary_exchange=args.primary_exchange,
                )
                if args.execute:
                    row["order"] = client.place_limit_order(
                        account,
                        args.symbol,
                        decision.action,
                        decision.quantity,
                        decision.limit_price,
                        primary_exchange=args.primary_exchange,
                        cancel_after=args.cancel_after,
                    )
                    row["executed"] = True

            decisions.append(row)
            if index + 1 < args.iterations:
                client.ib.sleep(args.interval)

    return {
        "account": account,
        "mode": "execute" if args.execute else "dry-run",
        "decisions": decisions,
    }


def cmd_leaps_once(settings: Settings, args: argparse.Namespace) -> Any:
    config = load_leaps_config(args)
    with IBKRClient(settings) as client:
        account = client.resolve_account()
        store = StateStore(args.state_dir, "leaps-overlay", account, config.symbol)
        trader = LeapsTrader(client, config, store, execute=args.execute)
        return trader.run_daily_cycle(force=args.force)


def cmd_run_leaps(settings: Settings, args: argparse.Namespace) -> None:
    config = load_leaps_config(args)
    while True:
        try:
            with IBKRClient(settings) as client:
                account = client.resolve_account()
                store = StateStore(args.state_dir, "leaps-overlay", account, config.symbol)
                trader = LeapsTrader(client, config, store, execute=args.execute)
                result = trader.run_daily_cycle(force=False)
                emit(result, json_output=args.json)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            print(f"run-leaps error: {message}", file=sys.stderr, flush=True)
            if getattr(args, "debug", False):
                traceback.print_exc()
        time.sleep(max(args.interval, 1.0))


def cmd_leaps_state(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    config = load_leaps_config(args)
    account = settings.account
    if not account:
        with IBKRClient(settings) as client:
            account = client.resolve_account()
    store = StateStore(args.state_dir, "leaps-overlay", account, config.symbol)
    state = store.load()
    return {
        "statePath": str(store.state_path),
        "journalPath": str(store.journal_path),
        "state": state,
    }


def cmd_leaps_reconcile(settings: Settings, args: argparse.Namespace) -> Any:
    config = load_leaps_config(args)
    with IBKRClient(settings) as client:
        account = client.resolve_account()
        store = StateStore(args.state_dir, "leaps-overlay", account, config.symbol)
        trader = LeapsTrader(client, config, store, execute=False)
        return trader.reconcile_state()


def cmd_leaps_example_config(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    return LeapsStrategyConfig().to_json_dict()


def cmd_systemd_unit(settings: Settings, args: argparse.Namespace) -> str:
    config = load_leaps_config(args)
    config_arg = f" --config {args.config}" if args.config else ""
    execute_arg = " --execute" if args.execute else ""
    env_arg = f" --env-file {args.env_file}" if args.env_file else ""
    account_arg = f" --account {settings.account}" if settings.account else ""
    command = (
        f"{args.python} -m ibkr_strategy_runner{env_arg}{account_arg} "
        f"run-leaps{config_arg} --state-dir {args.state_dir}{execute_arg}"
    )
    return "\n".join(
        [
            "[Unit]",
            "Description=ibkr-strategy-runner LEAPS overlay trading daemon",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={args.working_directory}",
            f"ExecStart={command}",
            "Restart=always",
            "RestartSec=60",
            "KillSignal=SIGINT",
            "",
            "[Install]",
            "WantedBy=default.target",
        ]
    )


def load_leaps_config(args: argparse.Namespace) -> LeapsStrategyConfig:
    if args.config:
        return LeapsStrategyConfig.from_file(args.config)
    return LeapsStrategyConfig()


def emit(value: Any, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(to_plain_data(value), indent=2, sort_keys=True))
        return

    if isinstance(value, Quote):
        print_table([to_plain_data(value)], ("symbol", "bid", "ask", "last", "close", "market_data_type"))
        return
    if isinstance(value, ThresholdDecision):
        print_table([to_plain_data(value)], ("symbol", "action", "price", "quantity", "limit_price", "reason"))
        return
    if isinstance(value, dict):
        print_dict(value)
        return
    if is_dataclass(value):
        print_dict(to_plain_data(value))
        return
    print(value)


def print_dict(value: dict[str, Any], indent: int = 0) -> None:
    prefix = " " * indent
    for key, item in value.items():
        if isinstance(item, list):
            print(f"{prefix}{key}:")
            if not item:
                print(f"{prefix}  none")
            elif all(isinstance(row, dict) for row in item):
                print_table(item, tuple(item[0].keys()), indent=indent + 2)
            else:
                for row in item:
                    print(f"{prefix}  {row}")
        elif isinstance(item, dict):
            print(f"{prefix}{key}:")
            print_dict(item, indent=indent + 2)
        else:
            print(f"{prefix}{key}: {item}")


def print_table(rows: list[dict[str, Any]], columns: tuple[str, ...], indent: int = 0) -> None:
    if not rows:
        print(" " * indent + "none")
        return

    widths = {
        column: max(len(column), *(len(format_cell(row.get(column))) for row in rows))
        for column in columns
    }
    prefix = " " * indent
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    separator = "  ".join("-" * widths[column] for column in columns)
    print(prefix + header)
    print(prefix + separator)
    for row in rows:
        print(prefix + "  ".join(format_cell(row.get(column)).ljust(widths[column]) for column in columns))


def format_cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def to_plain_data(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [to_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain_data(item) for key, item in value.items()}
    return value
