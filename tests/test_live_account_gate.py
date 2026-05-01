from __future__ import annotations

import unittest

from ibkr_strategy_runner.config import Settings
from ibkr_strategy_runner.ibkr import IBKRClient, is_paper_account


def make_settings(
    allow_live_trading: bool = False,
    live_account_allowlist: tuple[str, ...] = (),
) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=4002,
        client_id=201,
        account=None,
        allow_order=True,
        allow_live_trading=allow_live_trading,
        live_account_allowlist=live_account_allowlist,
        default_exchange="SMART",
        default_currency="USD",
        connect_timeout=10.0,
        request_timeout=15.0,
        market_data_type=3,
    )


class LiveAccountGateTest(unittest.TestCase):
    def test_identifies_paper_accounts(self) -> None:
        self.assertTrue(is_paper_account("DU123456"))
        self.assertFalse(is_paper_account("U123456"))

    def test_paper_account_passes_without_live_gate(self) -> None:
        client = IBKRClient(make_settings())
        client.require_trading_account("DU123456", require_cap=True)

    def test_live_account_is_blocked_by_default(self) -> None:
        client = IBKRClient(make_settings())

        with self.assertRaisesRegex(RuntimeError, "IB_ALLOW_LIVE_TRADING"):
            client.require_trading_account("U123456")

    def test_live_account_requires_allowlist(self) -> None:
        client = IBKRClient(make_settings(allow_live_trading=True))

        with self.assertRaisesRegex(RuntimeError, "IB_LIVE_ACCOUNT_ALLOWLIST"):
            client.require_trading_account("U123456")

    def test_live_strategy_execution_requires_cap(self) -> None:
        client = IBKRClient(
            make_settings(
                allow_live_trading=True,
                live_account_allowlist=("U123456",),
            )
        )

        with self.assertRaisesRegex(RuntimeError, "strategy_capital_limit"):
            client.require_trading_account("U123456", require_cap=True)

    def test_live_strategy_execution_passes_with_allowlist_and_cap(self) -> None:
        client = IBKRClient(
            make_settings(
                allow_live_trading=True,
                live_account_allowlist=("U123456",),
            )
        )

        client.require_trading_account(
            "U123456",
            strategy_capital_limit=5000.0,
            require_cap=True,
        )


if __name__ == "__main__":
    unittest.main()
