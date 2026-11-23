-- Shipments booked with the external carrier when an order is fulfilled.
CREATE TABLE fulfillments (
    id               BIGSERIAL PRIMARY KEY,
    order_id         BIGINT NOT NULL UNIQUE REFERENCES orders (id),
    carrier          TEXT NOT NULL,
    tracking_number  TEXT NOT NULL,
    shipped_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX orders_customer_status_idx ON orders (customer_id, status);
