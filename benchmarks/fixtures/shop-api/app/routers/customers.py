"""Customer endpoints."""

from fastapi import APIRouter, HTTPException

from app.cache import cached
from app.config import CUSTOMER_SUMMARY_TTL_SECONDS
from app.db import connection
from app.schemas import Customer, CustomerSummary
from app.services.pricing import cents_to_dollars

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("/{customer_id}", response_model=Customer)
def get_customer(customer_id: int) -> Customer:
    with connection() as conn:
        row = conn.execute(
            "SELECT id, email, full_name, tier FROM customers WHERE id = %s", (customer_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Customer not found")
    return Customer(**row)


@router.get("/{customer_id}/summary", response_model=CustomerSummary)
def get_customer_summary(customer_id: int) -> CustomerSummary:
    """Order count and lifetime value, cached in memory for 60 seconds per customer."""
    return cached(
        f"customer-summary:{customer_id}",
        CUSTOMER_SUMMARY_TTL_SECONDS,
        lambda: _load_summary(customer_id),
    )


def _load_summary(customer_id: int) -> CustomerSummary:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT count(*) AS order_count,
                   coalesce(sum(total_cents), 0) AS lifetime_cents,
                   max(created_at) AS last_order_at
            FROM orders
            WHERE customer_id = %s AND status IN ('paid', 'fulfilled')
            """,
            (customer_id,),
        ).fetchone()
    return CustomerSummary(
        customer_id=customer_id,
        order_count=row["order_count"],
        lifetime_value=cents_to_dollars(row["lifetime_cents"]),
        last_order_at=row["last_order_at"],
    )
