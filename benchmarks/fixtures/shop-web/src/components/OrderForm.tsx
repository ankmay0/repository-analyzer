import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { formatMoney } from "../format";

/** Pick products and quantities, then POST /orders. Out-of-stock products are hidden. */
export function OrderForm({ customerId }: { customerId: number }) {
  const queryClient = useQueryClient();
  const products = useQuery({ queryKey: ["products"], queryFn: api.products });
  const [cart, setCart] = useState<Record<string, number>>({});

  const place = useMutation({
    mutationFn: () =>
      api.createOrder(
        customerId,
        Object.entries(cart).filter(([, qty]) => qty > 0).map(([sku, quantity]) => ({ sku, quantity })),
      ),
    onSuccess: () => {
      setCart({});
      void queryClient.invalidateQueries({ queryKey: ["orders", customerId] });
      void queryClient.invalidateQueries({ queryKey: ["products"] });
    },
  });

  // Estimate only: tax, discount and shipping are computed by the server.
  const estimate = (products.data ?? []).reduce((sum, p) => sum + p.price * (cart[p.sku] ?? 0), 0);

  return (
    <form onSubmit={(e) => { e.preventDefault(); place.mutate(); }}>
      <h3>New order</h3>
      {(products.data ?? []).filter((p) => p.in_stock).map((p) => (
        <label key={p.sku}>
          {p.name} ({formatMoney(p.price)})
          <input
            type="number" min={0} max={100} value={cart[p.sku] ?? 0}
            onChange={(e) => setCart({ ...cart, [p.sku]: Number(e.target.value) })}
          />
        </label>
      ))}
      <p>Items subtotal (before tax and shipping): {formatMoney(estimate)}</p>
      <button type="submit" disabled={estimate === 0 || place.isPending}>Place order</button>
      {place.error && <p role="alert">{place.error.message}</p>}
    </form>
  );
}
