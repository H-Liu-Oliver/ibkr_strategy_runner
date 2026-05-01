from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    client_id: int
    account: str | None
    allow_order: bool
    allow_live_trading: bool
    live_account_allowlist: tuple[str, ...]
    default_exchange: str
    default_currency: str
    connect_timeout: float
    request_timeout: float
    market_data_type: int


def load_environment(explicit_env_file: str | None = None) -> Path | None:
    """Load the first available dotenv file and return the path that was used."""
    if explicit_env_file:
        path = Path(explicit_env_file).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Env file not found: {path}")
        load_dotenv(path, override=True)
        return path

    candidates = (
        Path.cwd() / ".env",
        Path.cwd() / "smoke" / ".env",
        Path.cwd().parent / "smoke" / ".env",
    )
    for path in candidates:
        if path.exists():
            load_dotenv(path, override=False)
            return path

    load_dotenv(override=False)
    return None


def settings_from_args(args: Any) -> Settings:
    load_environment(getattr(args, "env_file", None))

    return Settings(
        host=args.host or os.getenv("IB_HOST", "127.0.0.1"),
        port=_as_int(args.port, "IB_PORT", 4002),
        client_id=_as_int(args.client_id, "IB_CLIENT_ID", 201),
        account=args.account or _blank_to_none(os.getenv("IB_ACCOUNT")),
        allow_order=_as_bool(os.getenv("IB_ALLOW_ORDER", "false")),
        allow_live_trading=_as_bool(os.getenv("IB_ALLOW_LIVE_TRADING", "false")),
        live_account_allowlist=_as_csv_tuple(os.getenv("IB_LIVE_ACCOUNT_ALLOWLIST")),
        default_exchange=args.exchange or os.getenv("IB_DEFAULT_EXCHANGE", "SMART"),
        default_currency=args.currency or os.getenv("IB_DEFAULT_CURRENCY", "USD"),
        connect_timeout=_as_float(args.timeout, "IB_CONNECT_TIMEOUT", 10.0),
        request_timeout=_as_float(args.request_timeout, "IB_REQUEST_TIMEOUT", 15.0),
        market_data_type=_as_int(args.market_data_type, "IB_MARKET_DATA_TYPE", 3),
    )


def _as_int(value: int | str | None, env_name: str, default: int) -> int:
    raw = value if value is not None else os.getenv(env_name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{env_name} must be an integer, got {raw!r}") from exc


def _as_float(value: float | str | None, env_name: str, default: float) -> float:
    raw = value if value is not None else os.getenv(env_name, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{env_name} must be a number, got {raw!r}") from exc


def _as_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_csv_tuple(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
