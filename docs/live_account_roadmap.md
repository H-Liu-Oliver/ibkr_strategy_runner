# Live Account Readiness Roadmap

This roadmap defines the work needed before `ibkr-strategy-runner` should be
allowed to submit orders in a real IBKR account. Each stage should be completed,
tested, reviewed, and committed independently before moving to the next stage.

## Stage 1: Real-Account Safety Gate

Status: Implemented.

Goal: keep paper trading as the default and require explicit operator intent
before any real account can submit orders.

Required behavior:

- Paper accounts continue to work with `IB_ALLOW_ORDER=true`.
- Real accounts are blocked unless live trading is explicitly enabled.
- Real accounts must be listed in an allowlist.
- Live trading must require a configured strategy capital cap.
- Refusal messages must explain which gate failed.

Validation:

- Unit tests for paper account pass-through.
- Unit tests for live account blocked by default.
- Unit tests for live account allowlist.
- Unit tests for live account requiring capital cap.
- CLI import/help still works.

## Stage 2: Hard Risk Limits

Status: Implemented.

Goal: enforce local limits before an order can reach IBKR.

Planned limits:

- `max_single_order_value`
- `max_daily_order_count`
- `max_daily_notional`
- `max_total_open_order_value`
- `max_stock_position_value`
- `max_option_position_value`
- `max_option_bid_ask_spread_pct`

Validation:

- Unit tests for each limit.
- Dry-run output includes risk usage.
- Execute mode refuses orders that breach limits.

## Stage 3: Structured Order Lifecycle

Status: Implemented.

Goal: replace loose order dictionaries with durable typed order records.

Lifecycle states:

- `planned`
- `submitted`
- `pre_submitted`
- `partially_filled`
- `filled`
- `cancelled`
- `expired`
- `rejected`
- `unknown`

Validation:

- Unit tests for order transition rules.
- State serialization round trip.
- Reconciliation handles unknown or stale order states safely.

## Stage 4: Execution Reconciliation

Status: Implemented.

Goal: make startup and recovery deterministic after IBKR pauses, restarts, or
disconnects.

Required behavior:

- Reconcile before every strategy cycle.
- Fetch open orders, executions, and positions.
- Detect mismatches between IBKR and local state.
- Refuse new orders when reconciliation is inconsistent.
- Provide operator-facing recovery guidance.

Validation:

- Mocked tests for filled, partial, cancelled, expired, rejected, and missing
  orders.
- `leaps-reconcile` reports clear actions.

## Stage 5: Stale Order Policy

Status: Implemented.

Goal: define and enforce what happens to unfilled orders.

Planned policies:

- Leave DAY orders alone until IBKR expires them.
- Cancel stale bot orders before the next cycle.
- Replace stale orders only when explicitly enabled.

Validation:

- Tests for each policy.
- Documentation shows the default real-account behavior.

## Stage 6: Position Ownership Boundary

Status: Implemented.

Goal: ensure the daemon only manages positions it owns.

Required behavior:

- Use `orderRef` and persisted state to identify bot-owned orders.
- Never sell manually-created positions by default.
- Provide import/quarantine commands for operator-managed recovery.

Planned commands:

- `bot-orders`
- `bot-positions`
- `import-position`
- `quarantine-position`

Validation:

- Tests for manual positions being ignored.
- Tests for imported positions being managed only after explicit import.

## Stage 7: Operator Status Commands

Goal: provide one-command operational visibility.

Planned commands:

- `status`
- `risk`
- `journal`
- `fills`
- `doctor`

Validation:

- Commands work without placing orders.
- `doctor` clearly distinguishes configuration, IBKR connectivity, state, and
  service issues.

## Stage 8: Alerting

Goal: notify the operator about events that require attention.

Planned alert events:

- Bot start/stop.
- IBKR disconnect or reconnect.
- Cycle failure.
- Order submitted.
- Order rejected.
- Fill or partial fill.
- Reconciliation mismatch.
- Risk-limit breach.

Initial transports:

- stdout/log only.
- webhook transport for chat/email bridges.

Validation:

- Tests for alert event generation.
- Manual webhook dry run.

## Stage 9: Backtest-To-Live Parity

Goal: reduce drift between backtest assumptions and live strategy behavior.

Required behavior:

- Shared strategy configuration model where practical.
- Dry-run command that explains today's decision from historical data.
- Document differences that remain live-only.

Validation:

- Unit tests for config compatibility.
- Golden tests for decision explanations.

## Stage 10: Simulation Harness

Goal: test trading behavior without IBKR.

Required scenarios:

- Restart with pending order.
- Fill after restart.
- Partial fill.
- Rejection.
- Expired DAY order.
- Risk-limit block.
- Duplicate cycle prevention.

Validation:

- CI-friendly test suite with mocked IBKR client.

## Stage 11: Service Hardening

Goal: make the daemon and IBKR runtime easier to operate.

Required documentation:

- systemd service setup.
- IB Gateway/TWS startup and supervision.
- IBC or equivalent gateway supervisor notes.
- Log rotation.
- State backup.
- Emergency stop and manual cancel procedure.

Validation:

- Service template reviewed.
- Operator checklist updated.

## Stage 12: State Storage Upgrade

Goal: move from JSON files to SQLite for live auditability.

Planned tables:

- cycles
- orders
- fills
- positions
- alerts
- journal

Validation:

- Migration from current JSON state.
- Atomic writes and schema-version tests.

## Live Rollout Process

After the readiness stages above:

1. Paper dry-run only.
2. Paper execute with a small cap.
3. Real-account dry-run.
4. Real-account execute with a very small cap, for example `$500`.
5. Raise the cap only after fills and reconciliation match expectations.
