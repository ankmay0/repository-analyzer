/** Thin typed wrapper over shop-api. Every call goes to VITE_API_URL. */

const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface Customer { id: number; email: string; full_name: string; tier: "standard" | "gold" }
export interface CustomerSummary { customer_id: number; order_count: number; lifetime_value: number; last_order_at: string | null }
export interface Product { sku: string; name: string; price: number; in_stock: boolean }
export interface Order {
  id: number; customer_id: number; status: "pending" | "paid" | "fulfilled" | "cancelled";
  subtotal: number; discount: number; tax: number; shipping: number; total: number; created_at: string;
}
export interface Fulfillment { id: number; order_id: number; carrier: string; tracking_number: string; shipped_at: string }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  customer: (id: number) => call<Customer>(`/customers/${id}`),
  customerSummary: (id: number) => call<CustomerSummary>(`/customers/${id}/summary`),
  products: () => call<Product[]>("/products"),
  orders: (customerId: number) => call<Order[]>(`/orders?customer_id=${customerId}`),
  createOrder: (customerId: number, items: { sku: string; quantity: number }[]) =>
    call<Order>("/orders", { method: "POST", body: JSON.stringify({ customer_id: customerId, items }) }),
  fulfill: (orderId: number) => call<Fulfillment>(`/orders/${orderId}/fulfill`, { method: "POST" }),
};
