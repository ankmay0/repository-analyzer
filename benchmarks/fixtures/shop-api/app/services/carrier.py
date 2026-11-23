"""Client for the external carrier (shipping) HTTP API."""

import httpx

from app.config import CARRIER_API_KEY, CARRIER_API_URL, CARRIER_TIMEOUT_SECONDS

CARRIER_NAME = "ParcelPoint"


class CarrierError(Exception):
    """The carrier rejected the shipment or could not be reached."""


def book_shipment(order_id: int, address: dict[str, str], parcel_count: int) -> str:
    """Book a pickup with the carrier and return its tracking number.

    POST {CARRIER_API_URL}/v2/shipments with a bearer token. Retries once on a
    timeout; any non-2xx response raises ``CarrierError``.
    """
    payload = {"reference": f"order-{order_id}", "address": address, "parcels": parcel_count}
    headers = {"Authorization": f"Bearer {CARRIER_API_KEY}"}
    for attempt in range(2):
        try:
            response = httpx.post(
                f"{CARRIER_API_URL}/v2/shipments",
                json=payload,
                headers=headers,
                timeout=CARRIER_TIMEOUT_SECONDS,
            )
            break
        except httpx.TimeoutException as exc:
            if attempt == 1:
                raise CarrierError("Carrier timed out") from exc
    if response.status_code >= 300:
        raise CarrierError(f"Carrier returned HTTP {response.status_code}")
    return str(response.json()["tracking_number"])
