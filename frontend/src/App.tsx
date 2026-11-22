import { useCallback, useEffect, useState } from "react";

import { ApiError, getWorkspace, startIndexing, type Workspace } from "./api";
import { BenchmarkPage } from "./benchmark/BenchmarkPage";
import { Chat } from "./components/Chat";
import { RepositoryPanel } from "./components/RepositoryPanel";

const POLL_INTERVAL_MS = 1500;

type Tab = "chat" | "benchmark";

export function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("chat");
  // Mount the benchmark page on first visit, then keep it (and any run it follows) alive.
  const [benchmarkVisited, setBenchmarkVisited] = useState(false);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      setWorkspace(await getWorkspace(signal));
      setError(null);
    } catch (cause) {
      if (signal?.aborted) return;
      setError(cause instanceof ApiError ? cause.message : "Cannot reach the API server.");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  // Poll while an indexing job is running, and keep retrying while the server is
  // unreachable so the page recovers on its own once the backend is up.
  const shouldPoll = workspace?.job.state === "running" || error !== null;
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
    if (next === "benchmark") setBenchmarkVisited(true);
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>Repository Analyzer</h1>
        <nav className="tabs" role="tablist" aria-label="Sections">
          {(["chat", "benchmark"] as const).map((name) => (
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
              {name === "chat" ? "Chat" : "Benchmark"}
            </button>
          ))}
        </nav>
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
      {benchmarkVisited && (
        <main
          className="benchmark-main"
          role="tabpanel"
          id="panel-benchmark"
          aria-labelledby="tab-benchmark"
          hidden={tab !== "benchmark"}
        >
          <BenchmarkPage />
        </main>
      )}
    </div>
  );
}
