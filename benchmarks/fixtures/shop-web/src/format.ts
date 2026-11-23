/** The API returns dollars as numbers; the UI shows them as USD strings. */
const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function formatMoney(dollars: number): string {
  return USD.format(dollars);
}

export function formatDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString("en-US", { dateStyle: "medium" }) : "never";
}
