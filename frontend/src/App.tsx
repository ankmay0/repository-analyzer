import { useCallback, useEffect, useState } from "react";

import { ApiError, getWorkspace, startIndexing, type Workspace } from "./api";
import { Chat } from "./components/Chat";
import { RepositoryPanel } from "./components/RepositoryPanel";

const POLL_INTERVAL_MS = 1500;

export function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>Repository Analyzer</h1>
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
  );
}
