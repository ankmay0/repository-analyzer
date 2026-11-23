-- Core tables. All money is stored as integer cents.
CREATE TABLE customers (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    full_name   TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'standard' CHECK (tier IN ('standard', 'gold')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    sku               TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    stock_quantity    INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    active            BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE orders (
    id               BIGSERIAL PRIMARY KEY,
    customer_id      BIGINT NOT NULL REFERENCES customers (id),
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'paid', 'fulfilled', 'cancelled')),
    subtotal_cents   INTEGER NOT NULL,
    discount_cents   INTEGER NOT NULL DEFAULT 0,
    tax_cents        INTEGER NOT NULL,
    shipping_cents   INTEGER NOT NULL,
    total_cents      INTEGER NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    order_id          BIGINT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    sku               TEXT NOT NULL REFERENCES products (sku),
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents  INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku)
);
