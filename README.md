# ibkr-strategy-runner

`ibkr-strategy-runner` is a small IBKR paper-trading CLI and long-running strategy daemon.
It grew out of the smoke tests in `../smoke` and reuses the same validated IBKR
API flows: connect, account summary, market data, what-if/order submission, open
orders, and positions.

The main automated strategy is a persistent QQQ LEAPS overlay:

- DCA into QQQ stock over time.
- Open long-dated call options after down-day signals.
- Reconcile open orders and positions after restarts or IBKR reconnects.
- Store bot state and an append-only journal so operation can resume.

This project is currently designed for **IBKR paper accounts**.

## Safety Model

The CLI intentionally has several trade guards:

- It refuses to trade unless the selected account looks like an IBKR paper
  account, usually `DU...`, or live trading is explicitly enabled and
  allowlisted.
- Order submission requires `IB_ALLOW_ORDER=true`.
- Real-account order submission also requires `IB_ALLOW_LIVE_TRADING=true` and
  the account in `IB_LIVE_ACCOUNT_ALLOWLIST`.
- Real-account strategy execution requires `strategy_capital_limit` in the
  strategy config.
- Strategy commands default to dry-run unless `--execute` is passed.
- `leaps-once` and `run-leaps` complete at most one execute cycle per market bar
  unless `--force` is used.
- The daemon state is stored on disk, separate from transient process memory.

The service supervises the Python trading process only. IB Gateway or TWS must
also be running and logged in separately.

## Install

From this directory:

```bash
python -m pip install -e .
```

Or use the existing smoke virtualenv directly:

```bash
/home/hliu/proj/trading/ibkr/smoke/.venv/bin/python -m ibkr_strategy_runner --help
```

## Environment

The CLI loads environment variables from:

1. `--env-file`
2. `.env` in the current directory
3. `../smoke/.env` when run from this project layout

Example:

```bash
cp .env.example .env
```

Important variables:

```bash
IB_HOST=127.0.0.1
IB_PORT=4002
IB_CLIENT_ID=201
IB_ACCOUNT=
IB_ALLOW_ORDER=false
IB_ALLOW_LIVE_TRADING=false
IB_LIVE_ACCOUNT_ALLOWLIST=
IB_DEFAULT_EXCHANGE=SMART
IB_DEFAULT_CURRENCY=USD
IBKR_STRATEGY_RUNNER_STATE_DIR=/home/hliu/.local/state/ibkr-strategy-runner
```

Use `IB_ALLOW_ORDER=false` for setup and dry runs. Set it to `true` only when
you intentionally want paper orders submitted.

For real accounts, keep `IB_ALLOW_LIVE_TRADING=false` until the live-account
readiness checklist in `docs/live_account_roadmap.md` is complete. A real
account must also be listed in `IB_LIVE_ACCOUNT_ALLOWLIST`, and strategy
execution requires a hard `strategy_capital_limit`.

## Basic Checks

With IB Gateway/TWS running:

```bash
ibkr-strategy-runner connect
ibkr-strategy-runner account
ibkr-strategy-runner positions
ibkr-strategy-runner open-orders
ibkr-strategy-runner quote QQQ --primary-exchange NASDAQ
```

Use `--json` before the command for machine-readable output:

```bash
ibkr-strategy-runner --json account
ibkr-strategy-runner --json open-orders
```

## Strategy Config

Use `examples/leaps-overlay.example.json` as a template. Keep personal configs
such as `configs-QQQ.json` local; they are ignored by Git because they can
contain account-specific sizing and risk choices.

Core fields:

```json
{
  "symbol": "QQQ",
  "primary_exchange": "NASDAQ",
  "capital_base": "net_liquidation",
  "strategy_capital_limit": null,
  "buying_power_fraction": null,
  "equity_allocation": 0.7,
  "option_allocation": 0.25,
  "cash_buffer_allocation": 0.05,
  "dca_months": 14.0,
  "signal_drop": -0.01,
  "target_delta": 0.6,
  "dte_days": 540,
  "trade_fraction": 0.0125,
  "max_positions": 5,
  "stale_order_policy": "leave_until_expired",
  "max_single_order_value": null,
  "max_daily_order_count": null,
  "max_daily_notional": null,
  "max_total_open_order_value": null,
  "max_stock_position_value": null,
  "max_option_position_value": null,
  "max_option_bid_ask_spread_pct": null
}
```

Capital sizing:

- Default: size from `NetLiquidation`.
- Hard cap: set `"strategy_capital_limit": 500000`.
- Buying-power fraction: set `"capital_base": "buying_power_fraction"` and
  `"buying_power_fraction": 0.5`.
- Safer buying-power cap: combine both, for example 50% of buying power capped
  at `$500,000`.

The bot reports the effective `strategy_capital` in each non-skipped cycle.

Stale order policy:

- `leave_until_expired`: default; leave stale open DAY orders at IBKR and block
  replacement orders until they expire or reconcile terminal.
- `cancel_before_cycle`: in execute mode, cancel stale bot orders and wait until
  the next cycle before replacing them.
- `replace_after_cancel`: in execute mode, cancel stale bot orders and allow a
  same-cycle replacement. Use this only after testing the behavior in paper.

Risk limits:

- `max_single_order_value`: maximum notional for one order.
- `max_daily_order_count`: maximum number of bot orders for one market bar date.
- `max_daily_notional`: maximum bot order notional for one market bar date.
- `max_total_open_order_value`: maximum notional already open plus the next
  order.
- `max_stock_position_value`: maximum stock sleeve value after the next stock
  order.
- `max_option_position_value`: maximum managed option value after the next
  option order.
- `max_option_bid_ask_spread_pct`: maximum option bid/ask spread as a fraction
  of mid price.

Unset limits are disabled. Real-account rollout should set conservative values
before enabling live execution. Each cycle includes a `risk` info action with
current daily order usage, open-order notional, sleeve values, and the active
limits.

## Example: `configs-QQQ.json`

The local `configs-QQQ.json` file is a concrete QQQ strategy setup for this
machine. It is ignored by Git because it can contain personal account/risk
choices.

Current behavior:

- Symbol: `QQQ`
- Primary exchange: `NASDAQ`
- Capital base: `NetLiquidation`
- Hard strategy cap: not set by default
- Buying-power sizing: disabled by default
- Stock target: `70%` of effective strategy capital
- LEAPS option cap: `25%` of effective strategy capital
- Cash buffer: `5%` of effective strategy capital
- DCA period: `14` months, approximated as `294` trading days
- Down-day LEAPS trigger: daily return at or below `-1%`
- Target option: call near `540` DTE and `0.60` delta
- Per-option-entry budget: `1.25%` of effective strategy capital
- Max managed LEAPS positions: `5`

For an account with:

```text
NetLiquidation = 1,009,617.03
AvailableFunds = 1,007,096.10
BuyingPower = 4,028,384.40
```

the default config sizes from `NetLiquidation`, not `BuyingPower`:

```text
strategy_capital = 1,009,617.03
target QQQ stock value = 1,009,617.03 * 0.70 = 706,731.92
daily QQQ DCA budget = 706,731.92 / 294 = 2,403.85
single LEAPS entry budget = 1,009,617.03 * 0.0125 = 12,620.21
maximum managed LEAPS value = 1,009,617.03 * 0.25 = 252,404.26
cash buffer target = 1,009,617.03 * 0.05 = 50,480.85
```

This is why a typical first QQQ stock DCA action may be only a few shares even
when account buying power is much larger.

To cap this strategy at `$500,000`, set:

```json
"capital_base": "net_liquidation",
"strategy_capital_limit": 500000,
"buying_power_fraction": null
```

Then the sizing becomes:

```text
strategy_capital = min(NetLiquidation, 500,000)
target QQQ stock value = 350,000
daily QQQ DCA budget = 350,000 / 294 = 1,190.48
single LEAPS entry budget = 6,250
```

To size from 50% of buying power, set:

```json
"capital_base": "buying_power_fraction",
"strategy_capital_limit": null,
"buying_power_fraction": 0.5
```

That uses leverage-sensitive buying power, so a safer variant is:

```json
"capital_base": "buying_power_fraction",
"strategy_capital_limit": 500000,
"buying_power_fraction": 0.5
```

which means:

```text
strategy_capital = min(BuyingPower * 0.5, 500,000)
```

## LEAPS Strategy

The strategy is a stock-plus-options overlay. It uses one underlying symbol
(`QQQ` in the example config) and divides effective strategy capital into three
sleeves:

- `equity_allocation`: target stock exposure, default `70%`.
- `option_allocation`: maximum managed LEAPS option exposure, default `25%`.
- `cash_buffer_allocation`: capital left unused by the strategy, default `5%`.

Effective strategy capital is not necessarily the whole account. It is computed
from the configured capital base and optional hard cap:

```text
strategy_capital = selected account value
strategy_capital = min(strategy_capital, strategy_capital_limit)  # when set
```

For the default config:

```text
strategy_capital = NetLiquidation
target QQQ stock value = strategy_capital * 0.70
maximum managed LEAPS value = strategy_capital * 0.25
cash buffer target = strategy_capital * 0.05
```

The stock sleeve is built gradually:

```text
dca_days = dca_months * 21
daily stock budget = target QQQ stock value / dca_days
shares to buy = floor(daily stock budget / current QQQ price)
```

This is not a hard maximum share count. It is a dollar target. If QQQ rises and
the stock sleeve grows above target, the bot currently stops buying more QQQ but
does not sell stock to rebalance downward.

The options sleeve is opportunistic. A LEAPS entry is considered only when the
latest daily return is at or below `signal_drop`:

```text
daily_return = latest_close / previous_close - 1
triggered = daily_return <= signal_drop
```

When triggered, the bot looks for a call option:

- expiry closest to `dte_days`, default around `540` days;
- strike closest to `target_delta`, default `0.60`;
- order budget of `strategy_capital * trade_fraction`, default `1.25%`;
- no new entry if it would exceed `option_allocation`;
- no more than `max_positions` managed option positions.

Each cycle:

1. Fetch recent daily QQQ bars.
2. Compute daily return and historical volatility.
3. Reconcile bot state with IBKR open orders, positions, and recent executions.
4. DCA QQQ stock toward the configured equity allocation.
5. If the daily return is at or below `signal_drop`, select a call option near
   `dte_days` and `target_delta`.
6. Buy options within `trade_fraction`, `option_allocation`, and
   `max_positions`.
7. Close managed options using take-profit and max-holding rules.

Option exits:

- `+50%` within `120` days
- `+30%` within `180` days
- `+10%` within `270` days
- force close after `270` days

The strategy is intentionally simple and conservative for paper deployment. It
does not currently model taxes, dividends, slippage, portfolio margin effects,
assignment/exercise workflows, or cross-symbol diversification. It also does not
use buying power by default; using buying power requires explicit config.

## Dry Run

Run one strategy cycle without placing orders:

```bash
IB_ALLOW_ORDER=false ibkr-strategy-runner --json --debug leaps-once \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner \
  --force
```

Expected output includes:

- `signal`
- `actions`
- `mode: "dry-run"`
- `execute: false`

Dry runs should not create IBKR orders.

Explain today's historical-data signal without touching state:

```bash
ibkr-strategy-runner --json leaps-explain \
  --config configs-QQQ.json
```

`leaps-explain` uses the same `LeapsStrategyConfig` model and historical-bar
signal logic as live execution. It intentionally does not model live-only
behavior such as account sizing, current quote availability, option-chain
selection, order routing, fills, commissions, or broker rejections.

## Paper Execution

Submit paper orders only after dry-run output is understood:

```bash
IB_ALLOW_ORDER=true ibkr-strategy-runner --json --debug leaps-once \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner \
  --execute \
  --force
```

Then inspect:

```bash
ibkr-strategy-runner open-orders
ibkr-strategy-runner positions
ibkr-strategy-runner --json leaps-state \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner
```

## Long-Running Service

Operational checklist: [docs/operator_runbook.md](docs/operator_runbook.md).

The daemon command:

```bash
ibkr-strategy-runner --json run-leaps \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner \
  --execute
```

Install the user `systemd` service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/ibkr-strategy-runner-leaps.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ibkr-strategy-runner-leaps.service
```

After editing Python code:

```bash
systemctl --user restart ibkr-strategy-runner-leaps.service
```

After editing the service file:

```bash
systemctl --user daemon-reload
systemctl --user restart ibkr-strategy-runner-leaps.service
```

Show the exact service command:

```bash
systemctl --user cat ibkr-strategy-runner-leaps.service
```

## Monitoring

Service status and logs:

```bash
systemctl --user status ibkr-strategy-runner-leaps.service
journalctl --user -u ibkr-strategy-runner-leaps.service -f
journalctl --user -u ibkr-strategy-runner-leaps.service --since "today"
```

IBKR account/order status:

```bash
ibkr-strategy-runner account
ibkr-strategy-runner positions
ibkr-strategy-runner open-orders
```

Bot state and journal:

```bash
ibkr-strategy-runner --json leaps-state \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner

tail -n 50 /home/hliu/.local/state/ibkr-strategy-runner/leaps-overlay_*_QQQ.jsonl
```

SQLite state is available for live auditability:

```bash
ibkr-strategy-runner --json migrate-state-sqlite \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner

ibkr-strategy-runner --json status \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner \
  --state-backend sqlite
```

Set `IBKR_STRATEGY_RUNNER_STATE_BACKEND=sqlite` after migration to use SQLite
for the daemon and operator commands. Keep a backup of the JSON state until the
SQLite state has been reconciled against IBKR.

Bot-owned orders and managed positions:

```bash
ibkr-strategy-runner --json status \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner

ibkr-strategy-runner --json risk \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner

ibkr-strategy-runner --json bot-orders \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner

ibkr-strategy-runner --json bot-positions \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner
```

Recent journal events and recorded fills:

```bash
ibkr-strategy-runner --json journal \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner \
  --limit 20

ibkr-strategy-runner --json fills \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner
```

Environment/config/service health check:

```bash
ibkr-strategy-runner --json doctor \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner
```

Alerts are emitted as structured log lines for daemon start/stop, cycle
failures, submitted/rejected orders, fills, partial fills, reconciliation
mismatches, and risk-limit breaches. To also POST alerts to a webhook, set:

```bash
IBKR_STRATEGY_RUNNER_ALERT_WEBHOOK_URL=https://example.invalid/webhook
IBKR_STRATEGY_RUNNER_ALERT_WEBHOOK_TIMEOUT=5
```

Dry-run the alert path without sending a network request:

```bash
ibkr-strategy-runner --json alert-test \
  --webhook-url https://example.invalid/webhook \
  --dry-run
```

Important state fields:

- `last_cycle_date`: last completed execute cycle.
- `last_dry_run_cycle_date`: last completed dry-run cycle.
- `pending_orders`: bot orders still open at IBKR.
- `completed_orders`: bot orders no longer open, with fills when IBKR returned
  execution reports.
- `positions`: managed option positions tracked by the bot.

Order records use an explicit `lifecycle_state` so restarts can distinguish
`submitted`, `pre_submitted`, `partially_filled`, `filled`, `cancelled`,
`expired`, `rejected`, and `unknown` orders. `unknown` means the bot could not
prove the final broker state from open orders and execution reports; check IBKR
manually before replacing that order.

The daemon only manages option positions persisted in `positions` with
`status="OPEN"`. Manual IBKR positions that are not in bot state are ignored.
To explicitly let the bot manage an existing option position, import it:

```bash
ibkr-strategy-runner import-position \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner \
  --con-id 123456789 \
  --local-symbol "QQQ  270115C00500000" \
  --expiry 20270115 \
  --strike 500 \
  --right C \
  --quantity 1 \
  --entry-price 42.50 \
  --entry-date 2026-05-01
```

To stop the bot from managing a persisted position:

```bash
ibkr-strategy-runner quarantine-position \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner \
  --con-id 123456789
```

## Reconciliation And Recovery

Run reconciliation after IB Gateway/TWS pauses, reconnects, or restarts:

```bash
ibkr-strategy-runner --json leaps-reconcile \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner
```

This compares persisted bot state with IBKR positions, open orders, and recent
executions. Open bot orders remain in `pending_orders`; orders no longer open
move to `completed_orders` with a terminal or `unknown` lifecycle state.
Every reconcile run also emits a `SUMMARY` action. If `blocking` is true, the
daemon refuses to continue into new strategy orders until the reported mismatch
is resolved.

If IBKR is offline, the service will log errors and retry on the next interval.
It cannot trade while Gateway/TWS is down, but it should resume once IBKR is
available again.

## Manual Order Tools

Useful direct commands:

```bash
ibkr-strategy-runner what-if QQQ --action BUY --qty 1 --limit 1.00 --primary-exchange NASDAQ
IB_ALLOW_ORDER=true ibkr-strategy-runner order QQQ --action BUY --qty 1 --limit 1.00 --primary-exchange NASDAQ
ibkr-strategy-runner cancel --order-id 12345
```

Use these carefully. Manual orders may not be managed by the LEAPS daemon unless
they use the bot's `orderRef` conventions.

## Development

Local verification:

```bash
python -m compileall ibkr_strategy_runner
python -m unittest discover -s tests
python -m ibkr_strategy_runner --help
python -m ibkr_strategy_runner --json leaps-example-config
```

The test suite includes `ibkr_strategy_runner.simulation.SimulatedIBKRClient`,
a CI-friendly harness for restart, fill, partial fill, rejection, expiry,
risk-limit, and duplicate-cycle scenarios without connecting to IBKR.

The project ignores secrets, virtualenvs, bytecode, logs, local configs, and
runtime state through `.gitignore`.
