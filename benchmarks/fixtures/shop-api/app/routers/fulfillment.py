"""Fulfilment: ship a paid order through the external carrier."""

from fastapi import APIRouter, HTTPException

from app.cache import invalidate
from app.db import connection
from app.schemas import Fulfillment
from app.services.carrier import CARRIER_NAME, CarrierError, book_shipment

router = APIRouter(tags=["fulfillment"])

# Placeholder until addresses are stored; every parcel ships from the one warehouse.
WAREHOUSE_ADDRESS = {"line1": "1 Depot Road", "city": "Austin", "postcode": "78701"}


@router.post("/orders/{order_id}/fulfill", response_model=Fulfillment, status_code=201)
def fulfill_order(order_id: int) -> Fulfillment:
    """Only ``paid`` orders can be fulfilled.

    Books the shipment with the carrier first; if that fails nothing is written
    and the API answers 502. On success it inserts the fulfillment row, marks
    the order ``fulfilled`` and drops the customer's cached summary.
    """
    with connection() as conn:
        order = conn.execute(
            "SELECT id, customer_id, status FROM orders WHERE id = %s FOR UPDATE", (order_id,)
        ).fetchone()
        if order is None:
            raise HTTPException(404, "Order not found")
        if order["status"] != "paid":
            raise HTTPException(409, f"Order is {order['status']}, not paid")
        parcels = conn.execute(
            "SELECT coalesce(sum(quantity), 0) AS n FROM order_items WHERE order_id = %s",
            (order_id,),
        ).fetchone()["n"]

        try:
            tracking = book_shipment(order_id, WAREHOUSE_ADDRESS, parcel_count=parcels)
        except CarrierError as exc:
            raise HTTPException(502, str(exc)) from exc

        row = conn.execute(
            "INSERT INTO fulfillments (order_id, carrier, tracking_number) VALUES (%s, %s, %s) "
            "RETURNING id, order_id, carrier, tracking_number, shipped_at",
            (order_id, CARRIER_NAME, tracking),
        ).fetchone()
        conn.execute("UPDATE orders SET status = 'fulfilled' WHERE id = %s", (order_id,))
    invalidate(f"customer-summary:{order['customer_id']}")
    return Fulfillment(**row)


@router.get("/fulfillments/{fulfillment_id}", response_model=Fulfillment)
def get_fulfillment(fulfillment_id: int) -> Fulfillment:
    with connection() as conn:
        row = conn.execute(
            "SELECT id, order_id, carrier, tracking_number, shipped_at FROM fulfillments "
            "WHERE id = %s",
            (fulfillment_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Fulfillment not found")
    return Fulfillment(**row)
