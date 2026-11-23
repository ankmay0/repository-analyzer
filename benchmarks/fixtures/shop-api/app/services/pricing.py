"""Order pricing. Everything is integer cents until the API boundary."""

from dataclasses import dataclass

TAX_RATE = 0.0825  # flat sales tax applied after discount
FREE_SHIPPING_THRESHOLD_CENTS = 5_000  # $50.00, compared against the discounted subtotal
FLAT_SHIPPING_CENTS = 599  # $5.99
GOLD_DISCOUNT_RATE = 0.10  # gold-tier customers get 10% off the subtotal


@dataclass(frozen=True)
class Price:
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    shipping_cents: int
    total_cents: int


def price_order(lines: list[tuple[int, int]], customer_tier: str) -> Price:
    """Price an order from ``(unit_price_cents, quantity)`` lines.

    Order of operations: subtotal -> tier discount -> shipping (on the discounted
    subtotal) -> tax on the discounted subtotal (shipping is not taxed) -> total.
    Tax and discount are rounded half-up to the cent.
    """
    subtotal = sum(unit * quantity for unit, quantity in lines)
    discount = round(subtotal * GOLD_DISCOUNT_RATE) if customer_tier == "gold" else 0
    discounted = subtotal - discount
    shipping = 0 if discounted >= FREE_SHIPPING_THRESHOLD_CENTS else FLAT_SHIPPING_CENTS
    tax = int(discounted * TAX_RATE + 0.5)
    return Price(subtotal, discount, tax, shipping, discounted + tax + shipping)


def cents_to_dollars(cents: int) -> float:
    return round(cents / 100, 2)
