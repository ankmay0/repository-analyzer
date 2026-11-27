import { useCallback, useEffect, useState } from "react";

import { ApiError, getHealth, getWorkspace, startIndexing, type Workspace } from "./api";
import { ComparePage } from "./compare/ComparePage";
import { Chat } from "./components/Chat";
import { DirectPage } from "./components/DirectPage";
import { HealthStatus, type HealthView } from "./components/HealthStatus";
import { RepositoryPanel } from "./components/RepositoryPanel";

const POLL_INTERVAL_MS = 1500;

type Tab = "chat" | "direct" | "compare";

const TAB_LABEL: Record<Tab, string> = { chat: "Chat", direct: "Direct", compare: "Compare" };

export function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthView>({ kind: "loading" });
  const [tab, setTab] = useState<Tab>("chat");
  // Mount the Compare page on first visit, then keep it (and any run it follows) alive.
  const [visited, setVisited] = useState<ReadonlySet<Tab>>(new Set(["chat"]));

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      setWorkspace(await getWorkspace(signal));
      setError(null);
    } catch (cause) {
      if (signal?.aborted) return;
      setError(cause instanceof ApiError ? cause.message : "Cannot reach the API server.");
    }
    try {
      setHealth({ kind: "ok", health: await getHealth(signal) });
    } catch {
      if (!signal?.aborted) setHealth({ kind: "unavailable" });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  // Poll while an indexing job is running, and keep retrying while the server is
  // unreachable so the page recovers on its own once the backend is up.
  const shouldPoll =
    workspace?.job.state === "running" || error !== null || health.kind === "unavailable";
  useEffect(() => {
    if (!shouldPoll) return;
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [shouldPoll, refresh]);

  const handleIndex = useCallback(async (repositories: string[]) => {
    try {
      setWorkspace(await startIndexing(repositories));
      setError(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Cannot reach the API server.");
    }
  }, []);

  const selectTab = (next: Tab) => {
    setTab(next);
    setVisited((current) => new Set(current).add(next));
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>Repository Analyzer</h1>
        <nav className="tabs" role="tablist" aria-label="Sections">
          {(["chat", "direct", "compare"] as const).map((name) => (
            <button
              key={name}
              type="button"
              role="tab"
              id={`tab-${name}`}
              aria-selected={tab === name}
              aria-controls={`panel-${name}`}
              className={`tab${tab === name ? " tab-active" : ""}`}
              onClick={() => selectTab(name)}
            >
              {TAB_LABEL[name]}
            </button>
          ))}
        </nav>
        <HealthStatus view={health} />
      </header>
      <div className="layout" role="tabpanel" id="panel-chat" aria-labelledby="tab-chat" hidden={tab !== "chat"}>
        <aside className="sidebar">
          {error && (
            <p className="notice notice-error" role="alert">
              {error}
            </p>
          )}
          <RepositoryPanel workspace={workspace} onIndex={handleIndex} />
        </aside>
        <main className="main">
          <Chat workspace={workspace} />
        </main>
      </div>
      {visited.has("direct") && (
        <main
          className="page-main"
          role="tabpanel"
          id="panel-direct"
          aria-labelledby="tab-direct"
          hidden={tab !== "direct"}
        >
          <DirectPage workspace={workspace} />
        </main>
      )}
      {visited.has("compare") && (
        <main
          className="page-main"
          role="tabpanel"
          id="panel-compare"
          aria-labelledby="tab-compare"
          hidden={tab !== "compare"}
        >
          <ComparePage active={tab === "compare"} />
        </main>
      )}
    </div>
  );
}
