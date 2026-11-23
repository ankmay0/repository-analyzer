import { useState } from "react";

import { CustomerCard } from "./components/CustomerCard";
import { OrderForm } from "./components/OrderForm";
import { OrderTable } from "./components/OrderTable";

export function App() {
  const [customerId, setCustomerId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  return (
    <main>
      <form onSubmit={(e) => { e.preventDefault(); setCustomerId(Number(draft) || null); }}>
        <input placeholder="Customer ID" value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button type="submit">Open</button>
      </form>
      {customerId !== null && (
        <>
          <CustomerCard customerId={customerId} />
          <OrderForm customerId={customerId} />
          <OrderTable customerId={customerId} />
        </>
      )}
    </main>
  );
}
