import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Order } from "../api/client";
import { formatDate, formatMoney } from "../format";

const STATUS_LABEL: Record<Order["status"], string> = {
  pending: "Awaiting payment",
  paid: "Ready to ship",
  fulfilled: "Shipped",
  cancelled: "Cancelled",
};

/** A customer's orders (newest first, as returned by GET /orders), with a Ship button. */
export function OrderTable({ customerId }: { customerId: number }) {
  const queryClient = useQueryClient();
  const orders = useQuery({
    queryKey: ["orders", customerId],
    queryFn: () => api.orders(customerId),
    refetchInterval: 15_000,
  });
  const ship = useMutation({
    mutationFn: api.fulfill,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders", customerId] });
      void queryClient.invalidateQueries({ queryKey: ["customer-summary", customerId] });
    },
  });

  return (
    <table>
      <thead>
        <tr><th>#</th><th>Placed</th><th>Status</th><th>Total</th><th /></tr>
      </thead>
      <tbody>
        {(orders.data ?? []).map((order) => (
          <tr key={order.id}>
            <td>{order.id}</td>
            <td>{formatDate(order.created_at)}</td>
            <td>{STATUS_LABEL[order.status]}</td>
            <td>{formatMoney(order.total)}</td>
            <td>
              {order.status === "paid" && (
                <button onClick={() => ship.mutate(order.id)} disabled={ship.isPending}>Ship</button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
