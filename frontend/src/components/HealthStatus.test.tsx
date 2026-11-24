import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { Health } from "../api";
import { HealthStatus } from "./HealthStatus";

const HEALTH: Health = {
  status: "ok",
  llm_configured: true,
  llm_model: "claude-opus-5-5",
  ctags_available: true,
  ctags_version: "Universal Ctags 6.0.0",
  graph_available: false,
};

describe("HealthStatus", () => {
  it("renders nothing while the health request is in flight", () => {
    expect(renderToStaticMarkup(<HealthStatus view={{ kind: "loading" }} />)).toBe("");
  });

  it("shows a quiet message when the health request fails", () => {
    const html = renderToStaticMarkup(<HealthStatus view={{ kind: "unavailable" }} />);
    expect(html).toContain("Server status unavailable");
    expect(html).not.toContain("Claude");
  });

  it("lists Claude, Ctags and the code graph, with a hint when the graph is off", () => {
    const html = renderToStaticMarkup(<HealthStatus view={{ kind: "ok", health: HEALTH }} />);
    expect(html).toContain('aria-label="Server status"');
    expect(html).toContain("Claude configured");
    expect(html).toContain("Ctags available");
    expect(html).toContain("Code graph off — set RA_GRAPH_ENABLED");
    expect(html).toContain("health-ok");
    expect(html).toContain("health-off");
  });

  it("says the code graph is on when Neo4j is reachable", () => {
    const html = renderToStaticMarkup(
      <HealthStatus view={{ kind: "ok", health: { ...HEALTH, graph_available: true } }} />,
    );
    expect(html).toContain("Code graph on");
    expect(html).not.toContain("RA_GRAPH_ENABLED");
  });
});
