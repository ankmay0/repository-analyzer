import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { formatDate, formatMoney } from "../format";

/** Customer header: profile from /customers/:id plus totals from /customers/:id/summary. */
export function CustomerCard({ customerId }: { customerId: number }) {
  const customer = useQuery({ queryKey: ["customer", customerId], queryFn: () => api.customer(customerId) });
  const summary = useQuery({
    queryKey: ["customer-summary", customerId],
    queryFn: () => api.customerSummary(customerId),
    staleTime: 30_000,
  });

  if (customer.isLoading) return <p>Loading…</p>;
  if (customer.error || !customer.data) return <p role="alert">Customer not found</p>;
  const c = customer.data;
  return (
    <section className="card">
      <h2>
        {c.full_name} {c.tier === "gold" && <span className="badge">Gold</span>}
      </h2>
      <p>{c.email}</p>
      {summary.data && (
        <dl>
          <dt>Orders</dt><dd>{summary.data.order_count}</dd>
          <dt>Lifetime value</dt><dd>{formatMoney(summary.data.lifetime_value)}</dd>
          <dt>Last order</dt><dd>{formatDate(summary.data.last_order_at)}</dd>
        </dl>
      )}
    </section>
  );
}
