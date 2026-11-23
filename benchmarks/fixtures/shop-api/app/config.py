"""Settings read from the environment once at import time."""

import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://shop:shop@localhost:5432/shop")
CARRIER_API_URL = os.environ.get("CARRIER_API_URL", "https://carrier.example.com")
CARRIER_API_KEY = os.environ.get("CARRIER_API_KEY", "")
CARRIER_TIMEOUT_SECONDS = 10.0

# Seconds a cached value stays fresh. See app/cache.py.
CUSTOMER_SUMMARY_TTL_SECONDS = 60
PRODUCT_CATALOG_TTL_SECONDS = 300
