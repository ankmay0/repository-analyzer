"""Request and response bodies. Money leaves the API as dollars (float), not cents."""

from datetime import datetime

from pydantic import BaseModel, Field


class Customer(BaseModel):
    id: int
    email: str
    full_name: str
    tier: str


class CustomerSummary(BaseModel):
    customer_id: int
    order_count: int
    lifetime_value: float  # dollars, paid + fulfilled orders only
    last_order_at: datetime | None


class Product(BaseModel):
    sku: str
    name: str
    price: float  # dollars
    in_stock: bool


class OrderLine(BaseModel):
    sku: str
    quantity: int = Field(gt=0, le=100)


class CreateOrder(BaseModel):
    customer_id: int
    items: list[OrderLine] = Field(min_length=1)


class Order(BaseModel):
    id: int
    customer_id: int
    status: str
    subtotal: float
    discount: float
    tax: float
    shipping: float
    total: float
    created_at: datetime


class Fulfillment(BaseModel):
    id: int
    order_id: int
    carrier: str
    tracking_number: str
    shipped_at: datetime
