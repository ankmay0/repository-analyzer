import { useEffect, useMemo, useState } from "react";

import { ApiError, getImpact, type Edge, type Impact, type Workspace } from "../api";

const WIDTH = 320;
const HEIGHT = 240;
const NODE_RADIUS = 7;

interface Point {
  x: number;
  y: number;
}

/** Place repositories evenly on a circle; adequate for a handful of nodes. */
function layout(names: string[]): Map<string, Point> {
  const radius = Math.min(WIDTH, HEIGHT) / 2 - 44;
  return new Map(
    names.map((name, index) => {
      const angle = (2 * Math.PI * index) / names.length - Math.PI / 2;
      return [
        name,
        { x: WIDTH / 2 + radius * Math.cos(angle), y: HEIGHT / 2 + radius * Math.sin(angle) },
      ];
    }),
  );
}

/** One drawn link per ordered pair; runtime if any declaring manifest says so. */
function distinctLinks(edges: Edge[]): { source: string; target: string; runtime: boolean }[] {
  const links = new Map<string, { source: string; target: string; runtime: boolean }>();
  for (const edge of edges) {
    const key = `${edge.source_repo}\u0000${edge.target_repo}`;
    const existing = links.get(key);
    const runtime = edge.scope === "runtime";
    if (existing) existing.runtime ||= runtime;
    else links.set(key, { source: edge.source_repo, target: edge.target_repo, runtime });
  }
  return [...links.values()];
}

/**
 * A slightly curved arrow from `from` to `to`, shortened so it starts and ends
 * at the node edge. The curve keeps A -> B and B -> A visually distinct when
 * two repositories depend on each other.
 */
function linkPath(from: Point, to: Point): string {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = Math.hypot(dx, dy) || 1;
  const ux = dx / length;
  const uy = dy / length;
  const start = { x: from.x + ux * NODE_RADIUS, y: from.y + uy * NODE_RADIUS };
  const end = { x: to.x - ux * (NODE_RADIUS + 5), y: to.y - uy * (NODE_RADIUS + 5) };
  const bend = 14;
  const control = {
    x: (start.x + end.x) / 2 - uy * bend,
    y: (start.y + end.y) / 2 + ux * bend,
  };
  return `M ${start.x} ${start.y} Q ${control.x} ${control.y} ${end.x} ${end.y}`;
}

export function DependencyPanel({ workspace }: { workspace: Workspace }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [error, setError] = useState<string | null>(null);

  const names = useMemo(() => workspace.repositories.map((r) => r.name), [workspace]);
  const positions = useMemo(() => layout(names), [names]);
  const links = useMemo(() => distinctLinks(workspace.edges), [workspace]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    getImpact(selected, controller.signal)
      .then((result) => {
        setImpact(result);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof ApiError ? cause.message : "Could not load the impact.");
      });
    return () => controller.abort();
  }, [selected]);

  const shown = impact && impact.repo === selected ? impact : null;
  const statusOf = (name: string): "failing" | "impacted" | "weak" | "unaffected" | "neutral" => {
    if (!shown) return "neutral";
    if (name === shown.repo) return "failing";
    const hit = shown.impacted.find((item) => item.repo === name);
    if (!hit) return "unaffected";
    return hit.weak ? "weak" : "impacted";
  };

  return (
    <section>
      <h2>Dependencies</h2>
      <p className="muted">
        An arrow points from a repository to one it depends on. Select a repository to see what is
        affected if it fails.
      </p>

      <svg
        className="graph"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label="Dependency graph between the indexed repositories"
      >
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" className="graph-arrowhead" />
          </marker>
        </defs>
        {links.map((link) => {
          const from = positions.get(link.source);
          const to = positions.get(link.target);
          if (!from || !to) return null;
          return (
            <path
              key={`${link.source}->${link.target}`}
              d={linkPath(from, to)}
              className={link.runtime ? "graph-link" : "graph-link graph-link-weak"}
              markerEnd="url(#arrow)"
            />
          );
        })}
        {names.map((name) => {
          const point = positions.get(name);
          if (!point) return null;
          const labelBelow = point.y > HEIGHT / 2;
          return (
            <g
              key={name}
              className={`graph-node graph-node-${statusOf(name)}`}
              onClick={() => setSelected(name)}
            >
              <circle cx={point.x} cy={point.y} r={NODE_RADIUS} />
              <text x={point.x} y={point.y + (labelBelow ? 22 : -13)} textAnchor="middle">
                {name}
              </text>
            </g>
          );
        })}
      </svg>

      <label htmlFor="impact-repo">If this repository fails</label>
      <select
        id="impact-repo"
        value={selected ?? ""}
        onChange={(event) => setSelected(event.target.value || null)}
      >
        <option value="">Select a repository…</option>
        {names.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>

      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      {shown && <ImpactList impact={shown} />}
    </section>
  );
}

function ImpactList({ impact }: { impact: Impact }) {
  if (impact.impacted.length === 0) {
    return (
      <p className="notice">
        No indexed repository declares a dependency on <strong>{impact.repo}</strong>.
      </p>
    );
  }
  return (
    <ul className="impact-list">
      {impact.impacted.map((item) => (
        <li key={item.repo}>
          <strong>{item.repo}</strong>{" "}
          <span className={item.weak ? "badge badge-weak" : "badge badge-strong"}>
            {item.weak ? "dev / optional only" : "runtime"}
          </span>
          <div className="muted">
            {[item.repo, ...item.path.map((edge) => edge.target_repo)].join(" → ")}
          </div>
          <div className="muted">
            declared in{" "}
            {item.path.map((edge) => `${edge.source_repo}/${edge.manifest_path}`).join(", ")}
          </div>
        </li>
      ))}
    </ul>
  );
}
