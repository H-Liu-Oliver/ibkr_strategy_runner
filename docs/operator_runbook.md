# Operator Runbook

This runbook is the operational checklist for running `ibkr-strategy-runner`
against paper first, then a tightly capped real account.

## Systemd Service

Install or refresh the user service:

```bash
PY=/home/hliu/proj/trading/ibkr/ibkr_strategy_runner/.venv/bin/python
ENV=/home/hliu/proj/trading/ibkr/ibkr_strategy_runner/.env
CONFIG=/home/hliu/proj/trading/ibkr/ibkr_strategy_runner/configs-QQQ.json
STATE_DIR=/home/hliu/.local/state/ibkr-strategy-runner

$PY -m ibkr_strategy_runner --env-file "$ENV" --account DUO585078 service install \
  --config "$CONFIG" \
  --state-dir "$STATE_DIR" \
  --state-backend sqlite \
  --execute \
  --python "$PY" \
  --working-directory /home/hliu/proj/trading/ibkr/ibkr_strategy_runner \
  --now
```

After Python code changes:

```bash
ibkr-strategy-runner service restart
```

After service setting changes, rerun `service install` with the intended
arguments and `--now`.

Confirm the exact command and current process state:

```bash
ibkr-strategy-runner service cat
ibkr-strategy-runner service status
```

Use the operator dashboard for a combined service, state, risk, and IBKR view:

```bash
ibkr-strategy-runner --json ops \
  --config "$CONFIG" \
  --state-dir "$STATE_DIR" \
  --state-backend sqlite
```

## IB Gateway Or TWS

The systemd unit supervises only the Python strategy daemon. IB Gateway or TWS
must be running, logged in, API-enabled, and reachable at the configured host
and port before the daemon can trade.

Before enabling execute mode:

```bash
ibkr-strategy-runner connect
ibkr-strategy-runner account
ibkr-strategy-runner open-orders
```

For unattended operation, use IBC or an equivalent supervisor to start IB
Gateway/TWS, keep it logged in, and restart it after maintenance windows. Keep
the gateway/TWS supervisor separate from the strategy service so a gateway
restart does not mutate bot state.

## Logs

Follow live logs:

```bash
journalctl --user -u ibkr-strategy-runner-leaps.service -f
```

Review recent logs:

```bash
ibkr-strategy-runner service logs --since "today" --lines 200
```

Limit retained user journal size if needed:

```bash
journalctl --user --vacuum-size=500M
```

## State Backup

State and journal files are the bot's durable memory. Back them up before
real-account rollout, before config changes, and after any manual recovery.

```bash
STATE_DIR=/home/hliu/.local/state/ibkr-strategy-runner
BACKUP_DIR=/home/hliu/.local/state/ibkr-strategy-runner-backups
mkdir -p "$BACKUP_DIR"
tar -C "$STATE_DIR" -czf "$BACKUP_DIR/state-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" .
```

Confirm current bot state:

```bash
ibkr-strategy-runner --json status --config configs-QQQ.json --state-dir "$STATE_DIR"
ibkr-strategy-runner --json bot-orders --config configs-QQQ.json --state-dir "$STATE_DIR"
ibkr-strategy-runner --json bot-positions --config configs-QQQ.json --state-dir "$STATE_DIR"
```

## Emergency Stop

Stop the strategy first:

```bash
ibkr-strategy-runner service stop
```

Then inspect and cancel live IBKR orders manually:

```bash
ibkr-strategy-runner open-orders
ibkr-strategy-runner cancel --order-id 12345
```

After manual intervention, reconcile before restarting:

```bash
ibkr-strategy-runner --json leaps-reconcile \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner

ibkr-strategy-runner --json doctor \
  --config configs-QQQ.json \
  --state-dir /home/hliu/.local/state/ibkr-strategy-runner
```

Restart only when `doctor`, `status`, `bot-orders`, IBKR `open-orders`, and
IBKR `positions` all match the intended recovery state.
