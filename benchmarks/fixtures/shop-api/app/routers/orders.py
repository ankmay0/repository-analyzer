"""Order endpoints."""

from fastapi import APIRouter, HTTPException, Query

from app.cache import invalidate
from app.db import connection
from app.schemas import CreateOrder, Order
from app.services.pricing import cents_to_dollars, price_order

router = APIRouter(prefix="/orders", tags=["orders"])

_ORDER_COLUMNS = (
    "id, customer_id, status, subtotal_cents, discount_cents, tax_cents, "
    "shipping_cents, total_cents, created_at"
)


def to_order(row: dict) -> Order:
    return Order(
        id=row["id"],
        customer_id=row["customer_id"],
        status=row["status"],
        subtotal=cents_to_dollars(row["subtotal_cents"]),
        discount=cents_to_dollars(row["discount_cents"]),
        tax=cents_to_dollars(row["tax_cents"]),
        shipping=cents_to_dollars(row["shipping_cents"]),
        total=cents_to_dollars(row["total_cents"]),
        created_at=row["created_at"],
    )


@router.post("", response_model=Order, status_code=201)
def create_order(body: CreateOrder) -> Order:
    """Price the cart, reserve stock and insert the order in one transaction."""
    with connection() as conn:
        customer = conn.execute(
            "SELECT tier FROM customers WHERE id = %s", (body.customer_id,)
        ).fetchone()
        if customer is None:
            raise HTTPException(404, "Customer not found")

        lines: list[tuple[str, int, int]] = []
        for item in body.items:
            # Lock the product row so two orders cannot oversell the same stock.
            product = conn.execute(
                "SELECT unit_price_cents, stock_quantity FROM products "
                "WHERE sku = %s AND active FOR UPDATE",
                (item.sku,),
            ).fetchone()
            if product is None:
                raise HTTPException(422, f"Unknown product {item.sku}")
            if product["stock_quantity"] < item.quantity:
                raise HTTPException(409, f"Not enough stock for {item.sku}")
            lines.append((item.sku, product["unit_price_cents"], item.quantity))

        price = price_order([(unit, qty) for _, unit, qty in lines], customer["tier"])
        order = conn.execute(
            f"""
            INSERT INTO orders (customer_id, subtotal_cents, discount_cents, tax_cents,
                                shipping_cents, total_cents)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_ORDER_COLUMNS}
            """,
            (
                body.customer_id,
                price.subtotal_cents,
                price.discount_cents,
                price.tax_cents,
                price.shipping_cents,
                price.total_cents,
            ),
        ).fetchone()
        for sku, unit, qty in lines:
            conn.execute(
                "INSERT INTO order_items (order_id, sku, quantity, unit_price_cents) "
                "VALUES (%s, %s, %s, %s)",
                (order["id"], sku, qty, unit),
            )
            conn.execute(
                "UPDATE products SET stock_quantity = stock_quantity - %s WHERE sku = %s",
                (qty, sku),
            )
    invalidate("products:")
    return to_order(order)


@router.get("", response_model=list[Order])
def list_orders(
    customer_id: int | None = None,
    status: str | None = Query(default=None, pattern="^(pending|paid|fulfilled|cancelled)$"),
    limit: int = Query(default=50, le=200),
) -> list[Order]:
    """Newest first. Not cached: reads the orders table on every call."""
    filters, params = [], []
    if customer_id is not None:
        filters.append("customer_id = %s")
        params.append(customer_id)
    if status is not None:
        filters.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders {where} ORDER BY created_at DESC LIMIT %s",
            (*params, limit),
        ).fetchall()
    return [to_order(row) for row in rows]


@router.get("/{order_id}", response_model=Order)
def get_order(order_id: int) -> Order:
    with connection() as conn:
        row = conn.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = %s", (order_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Order not found")
    return to_order(row)


@router.post("/{order_id}/pay", response_model=Order)
def mark_order_paid(order_id: int) -> Order:
    """Called by the payment webhook: ``pending`` -> ``paid``. Anything else is a 409."""
    with connection() as conn:
        row = conn.execute(
            f"UPDATE orders SET status = 'paid' WHERE id = %s AND status = 'pending' "
            f"RETURNING {_ORDER_COLUMNS}",
            (order_id,),
        ).fetchone()
        if row is None:
            exists = conn.execute("SELECT 1 FROM orders WHERE id = %s", (order_id,)).fetchone()
            raise HTTPException(409 if exists else 404, "Order is not pending" if exists else "Order not found")
    invalidate(f"customer-summary:{row['customer_id']}")
    return to_order(row)
