from __future__ import annotations

from .models import Quote, ThresholdDecision


def evaluate_threshold(
    symbol: str,
    quote: Quote,
    quantity: float,
    buy_below: float | None,
    sell_above: float | None,
    limit_offset: float = 0.0,
) -> ThresholdDecision:
    price = quote.usable_price
    if price is None:
        return ThresholdDecision(
            symbol=symbol.upper(),
            action="HOLD",
            reason="no usable price",
            price=None,
            quantity=quantity,
            limit_price=None,
        )

    if buy_below is not None and price <= buy_below:
        limit_price = max(round(price + limit_offset, 2), 0.01)
        return ThresholdDecision(
            symbol=symbol.upper(),
            action="BUY",
            reason=f"price {price:.2f} <= buy_below {buy_below:.2f}",
            price=price,
            quantity=quantity,
            limit_price=limit_price,
        )

    if sell_above is not None and price >= sell_above:
        limit_price = max(round(price + limit_offset, 2), 0.01)
        return ThresholdDecision(
            symbol=symbol.upper(),
            action="SELL",
            reason=f"price {price:.2f} >= sell_above {sell_above:.2f}",
            price=price,
            quantity=quantity,
            limit_price=limit_price,
        )

    return ThresholdDecision(
        symbol=symbol.upper(),
        action="HOLD",
        reason="thresholds not crossed",
        price=price,
        quantity=quantity,
        limit_price=None,
    )
