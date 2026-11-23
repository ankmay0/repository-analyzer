# shop-api (benchmark fixture)

A deliberately small order-management backend used as a **known-answer test app**
for Repository Analyzer benchmarks. It is not meant to be deployed.

FastAPI + PostgreSQL (psycopg 3). Customers place orders for products; orders are
fulfilled by booking a shipment with an external carrier API.

    DATABASE_URL=postgresql://shop:shop@localhost:5432/shop
    CARRIER_API_URL=https://carrier.example.com
    uvicorn app.main:app --reload

Apply `migrations/*.sql` in order before starting.
