from app.services.pricing import price_order


def test_standard_customer_under_threshold_pays_shipping() -> None:
    price = price_order([(1_000, 2)], "standard")
    assert (price.subtotal_cents, price.discount_cents, price.shipping_cents) == (2_000, 0, 599)
    assert price.tax_cents == 165
    assert price.total_cents == 2_000 + 165 + 599


def test_gold_discount_applies_before_shipping_threshold() -> None:
    # $54.00 subtotal, 10% off -> $48.60, which is under $50, so shipping is charged.
    price = price_order([(5_400, 1)], "gold")
    assert price.discount_cents == 540
    assert price.shipping_cents == 599
