from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    close: float | None
    market_data_type: int | None

    @property
    def usable_price(self) -> float | None:
        for price in (self.last, self.close, self.ask, self.bid):
            if price is not None:
                return price
        return None


@dataclass(frozen=True)
class ThresholdDecision:
    symbol: str
    action: str
    reason: str
    price: float | None
    quantity: float
    limit_price: float | None
