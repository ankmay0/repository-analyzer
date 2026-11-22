"""Application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import pool
from app.routers import customers, fulfillment, orders, products


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    yield
    pool.close()


app = FastAPI(title="shop-api", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5174"], allow_methods=["*"])
app.include_router(customers.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(fulfillment.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
