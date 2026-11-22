"""Product catalog."""

from fastapi import APIRouter

from app.cache import cached
from app.config import PRODUCT_CATALOG_TTL_SECONDS
from app.db import connection
from app.schemas import Product
from app.services.pricing import cents_to_dollars

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[Product])
def list_products() -> list[Product]:
    """Active products only, cached for 5 minutes for all callers."""
    return cached("products:active", PRODUCT_CATALOG_TTL_SECONDS, _load_active_products)


def _load_active_products() -> list[Product]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT sku, name, unit_price_cents, stock_quantity FROM products "
            "WHERE active ORDER BY name"
        ).fetchall()
    return [
        Product(
            sku=row["sku"],
            name=row["name"],
            price=cents_to_dollars(row["unit_price_cents"]),
            in_stock=row["stock_quantity"] > 0,
        )
        for row in rows
    ]
