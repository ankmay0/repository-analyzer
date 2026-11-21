import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  getImpact,
  type Diagram,
  type DiagramNodeStatus,
  type Impact,
  type Workspace,
} from "../api";
import { GraphView } from "./GraphView";

/** The repository dependency graph, marked up with the impact of a failure if one is shown. */
function toDiagram(workspace: Workspace, impact: Impact | null): Diagram {
  const statusOf = (name: string): DiagramNodeStatus => {
    if (!impact) return "normal";
    if (name === impact.repo) return "failing";
    const hit = impact.impacted.find((item) => item.repo === name);
    if (!hit) return "normal";
    return hit.weak ? "weak" : "affected";
  };

  // One arrow per pair of repositories; solid if any manifest makes it a runtime dependency.
  const runtime = new Map<string, { source: string; target: string; runtime: boolean }>();
  for (const edge of workspace.edges) {
    const key = `${edge.source_repo}\u0000${edge.target_repo}`;
    const existing = runtime.get(key);
    const isRuntime = edge.scope === "runtime";
    if (existing) existing.runtime ||= isRuntime;
    else runtime.set(key, { source: edge.source_repo, target: edge.target_repo, runtime: isRuntime });
  }

  return {
    title: "Dependency graph between the indexed repositories",
    nodes: workspace.repositories.map((repository) => ({
      id: repository.name,
      label: repository.name,
      kind: "repository",
      status: statusOf(repository.name),
    })),
    edges: [...runtime.values()].map((link) => ({
      source: link.source,
      target: link.target,
      label: "",
      dashed: !link.runtime,
    })),
  };
}

export function DependencyPanel({ workspace }: { workspace: Workspace }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [error, setError] = useState<string | null>(null);

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
  const diagram = useMemo(() => toDiagram(workspace, shown), [workspace, shown]);

  return (
    <section>
      <h2>Dependencies</h2>
      <p className="muted">
        An arrow points from a repository to one it depends on; dashed means development or
        optional only. Select a repository to see what is affected if it fails.
      </p>

      <GraphView diagram={diagram} direction="TB" height={280} onNodeClick={setSelected} />

      <label htmlFor="impact-repo">If this repository fails</label>
      <select
        id="impact-repo"
        value={selected ?? ""}
        onChange={(event) => setSelected(event.target.value || null)}
      >
        <option value="">Select a repository…</option>
        {workspace.repositories.map((repository) => (
          <option key={repository.name} value={repository.name}>
            {repository.name}
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
