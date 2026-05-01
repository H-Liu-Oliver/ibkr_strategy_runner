from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, TextIO

from .live_state import utc_now_iso


@dataclass(frozen=True)
class AlertEvent:
    event_type: str
    severity: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utc_now_iso)


class AlertSink:
    def __init__(
        self,
        webhook_url: str | None = None,
        webhook_timeout: float = 5.0,
        dry_run: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.webhook_timeout = webhook_timeout
        self.dry_run = dry_run
        self.stream = stream if stream is not None else sys.stderr

    @classmethod
    def from_env(
        cls,
        webhook_url: str | None = None,
        dry_run: bool = False,
        stream: TextIO | None = None,
    ) -> "AlertSink":
        timeout = float(os.getenv("IBKR_STRATEGY_RUNNER_ALERT_WEBHOOK_TIMEOUT", "5"))
        return cls(
            webhook_url=webhook_url or os.getenv("IBKR_STRATEGY_RUNNER_ALERT_WEBHOOK_URL"),
            webhook_timeout=timeout,
            dry_run=dry_run,
            stream=stream,
        )

    def emit(self, event: AlertEvent) -> dict[str, Any]:
        payload = asdict(event)
        print(json.dumps({"alert": payload}, sort_keys=True), file=self.stream, flush=True)
        delivered = False
        if self.webhook_url and not self.dry_run:
            request = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.webhook_timeout) as response:
                response.read()
            delivered = True
        return {
            "event": payload,
            "webhookUrl": self.webhook_url,
            "webhookDelivered": delivered,
            "dryRun": self.dry_run,
        }

    def emit_many(self, events: list[AlertEvent]) -> list[dict[str, Any]]:
        return [self.emit(event) for event in events]


def alert_events_from_cycle(result: Any) -> list[AlertEvent]:
    events: list[AlertEvent] = []
    for action in getattr(result, "actions", []):
        action_type = action.get("type")
        action_name = action.get("action")
        reason = str(action.get("reason") or "")

        if action.get("blocking") and action_type == "reconcile":
            events.append(
                AlertEvent(
                    "reconciliation_mismatch",
                    "warning",
                    reason or "reconciliation blocked trading",
                    {"action": action},
                )
            )

        if "risk limit" in reason:
            events.append(
                AlertEvent(
                    "risk_limit_breach",
                    "warning",
                    reason,
                    {"action": action},
                )
            )

        if action_name in {"BUY", "SELL"} and action.get("execute") and action.get("order"):
            order = action["order"]
            status = str(order.get("status") or "")
            severity = "error" if status.lower() in {"inactive", "validationerror"} else "info"
            event_type = "order_rejected" if severity == "error" else "order_submitted"
            events.append(
                AlertEvent(
                    event_type,
                    severity,
                    f"{action_name} order {status or 'submitted'} for {action.get('symbol') or action.get('local_symbol')}",
                    {"action": action},
                )
            )

        if action_name == "ORDER_PARTIALLY_FILLED":
            events.append(
                AlertEvent(
                    "partial_fill",
                    "info",
                    "bot order was partially filled",
                    {"action": action},
                )
            )

        if action_name in {"PENDING_ORDER_CLEARED", "ORDER_TERMINAL"} and action.get("fills"):
            events.append(
                AlertEvent(
                    "fill",
                    "info",
                    "bot order has fill reports",
                    {"action": action},
                )
            )
    return events


def daemon_event(event_type: str, message: str, severity: str = "info", **payload: Any) -> AlertEvent:
    return AlertEvent(event_type, severity, message, payload)


def failure_event(exc: Exception, context: str) -> AlertEvent:
    return AlertEvent(
        "cycle_failure",
        "error",
        str(exc) or exc.__class__.__name__,
        {"context": context, "errorType": exc.__class__.__name__},
    )
