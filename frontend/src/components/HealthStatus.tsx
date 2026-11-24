import type { Health } from "../api";

export type HealthView =
  | { kind: "loading" }
  | { kind: "ok"; health: Health }
  | { kind: "unavailable" };

function Item({ ok, children }: { ok: boolean; children: string }) {
  return <li className={ok ? "health-ok" : "health-off"}>{children}</li>;
}

/** Compact Claude / Ctags / code-graph status from GET /api/health. */
export function HealthStatus({ view }: { view: HealthView }) {
  if (view.kind === "loading") return null;
  if (view.kind === "unavailable") {
    return <p className="health-status health-unavailable">Server status unavailable</p>;
  }

  const { health } = view;
  return (
    <ul className="health-status" aria-label="Server status">
      <Item ok={health.llm_configured}>
        {health.llm_configured ? "Claude configured" : "Claude not configured"}
      </Item>
      <Item ok={health.ctags_available}>{health.ctags_available ? "Ctags available" : "Ctags missing"}</Item>
      <Item ok={health.graph_available}>
        {health.graph_available ? "Code graph on" : "Code graph off — set RA_GRAPH_ENABLED"}
      </Item>
    </ul>
  );
}
