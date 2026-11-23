import { type FormEvent, type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "../api";
import { getCompareStatus, streamCompare } from "./api";
import { CompareColumn } from "./CompareColumn";
import {
  applyCompareEvent,
  emptyColumn,
  finishRun,
  highlights,
  isRunning,
  loadHistory,
  MAX_HISTORY,
  newRun,
  saveHistory,
  type CompareRun,
} from "./model";
import type { CompareStatus } from "./types";

const EXAMPLES = [
  "How is an order's total price calculated?",
  "Where does the customer summary come from, and how is it cached?",
];

function describe(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : "The connection to the server was lost.";
}

export function ComparePage({ active }: { active: boolean }) {
  const [status, setStatus] = useState<CompareStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [repository, setRepository] = useState("");
  const [question, setQuestion] = useState("");
  const [runs, setRuns] = useState<CompareRun[]>(() => loadHistory(sessionStorage));
  const [shownId, setShownId] = useState<number | null>(null);
  const nextId = useRef(Math.max(0, ...runs.map((r) => r.id)) + 1);
  const abortRef = useRef<AbortController | null>(null);

  const shown = runs.find((r) => r.id === (shownId ?? runs[0]?.id));
  const running = runs.some(isRunning);

  // Reload the repositories whenever the tab is opened: they may have been indexed since.
  useEffect(() => {
    if (!active || running) return;
    const controller = new AbortController();
    getCompareStatus(controller.signal)
      .then((loaded) => {
        setStatus(loaded);
        setStatusError(null);
        setRepository((current) =>
          loaded.repositories.some((r) => r.id === current) ? current : (loaded.repositories[0]?.id ?? ""),
        );
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setStatusError(describe(cause));
      });
    return () => controller.abort();
  }, [active, running]);

  useEffect(() => saveHistory(sessionStorage, runs), [runs]);
  useEffect(() => () => abortRef.current?.abort(), []);

  const ask = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      const target = status?.repositories.find((r) => r.id === repository);
      if (!trimmed || running || !status || !target) return;
      const id = nextId.current++;
      const update = (change: (run: CompareRun) => CompareRun) =>
        setRuns((current) => current.map((run) => (run.id === id ? change(run) : run)));
      const initial = {
        ...newRun(id, trimmed, target.id, target.label),
        columns: status.approaches.map(emptyColumn),
      };
      setRuns((current) => [initial, ...current].slice(0, MAX_HISTORY));
      setShownId(id);
      setQuestion("");

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        for await (const event of streamCompare({ question: trimmed, repository: target.id }, controller.signal)) {
          update((run) => applyCompareEvent(run, event));
        }
        update((run) => (run.finished ? run : finishRun(run)));
      } catch (cause) {
        update((run) => finishRun(run, controller.signal.aborted ? "Stopped." : describe(cause)));
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [status, repository, running],
  );

  const stop = () => abortRef.current?.abort();

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void ask(question);
    }
  }

  const columns = useMemo(
    () => shown?.columns ?? status?.approaches.map(emptyColumn) ?? [],
    [shown, status],
  );
  const marks = useMemo(() => highlights(columns), [columns]);
  const blocked = status && !status.llm_configured ? status.llm_message : null;
  const canAsk = !!status && blocked === null && repository !== "" && question.trim() !== "" && !running;

  return (
    <div className="compare">
      <div className="benchmark-intro">
        <h2>Compare approaches</h2>
        <p className="muted">
          Ask one question and see how each approach answers, side by side, with its token usage and latency.
          {status ? ` Model: ${status.llm_model}.` : ""}
        </p>
      </div>
      {(statusError || blocked) && (
        <p className="notice notice-error" role="alert">
          {statusError ?? blocked}
        </p>
      )}

      <form className="compare-form" onSubmit={handleSubmit}>
        <div className="compare-form-row">
          <div className="compare-repo">
            <label htmlFor="compare-repository">Repository</label>
            <select
              id="compare-repository"
              value={repository}
              disabled={running || !status}
              onChange={(event) => setRepository(event.target.value)}
            >
              {status?.repositories.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>
          <div className="compare-question">
            <label htmlFor="compare-question">Question</label>
            <textarea
              id="compare-question"
              rows={2}
              value={question}
              placeholder="Ask anything about the repository"
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={!status || blocked !== null}
            />
          </div>
          {running ? (
            <button type="button" className="secondary" onClick={stop}>
              Stop
            </button>
          ) : (
            <button type="submit" disabled={!canAsk}>
              Compare
            </button>
          )}
        </div>
        {runs.length === 0 && status && blocked === null && (
          <p className="compare-examples">
            Try:{" "}
            {EXAMPLES.map((example) => (
              <button key={example} type="button" className="secondary" onClick={() => setQuestion(example)}>
                {example}
              </button>
            ))}
          </p>
        )}
      </form>

      {shown && (
        <p className="compare-asked" aria-live="polite">
          <strong>{shown.question}</strong> <span className="muted">· {shown.label}</span>
          {shown.error && <span className="muted"> · {shown.error}</span>}
        </p>
      )}

      <div className="compare-columns">
        {columns.map((column) => (
          <CompareColumn
            key={column.name}
            column={column}
            fewestTokens={marks.fewestTokens.has(column.name)}
            fastest={marks.fastest.has(column.name)}
          />
        ))}
      </div>

      {runs.length > 0 && (
        <section className="compare-history" aria-label="History">
          <h3>This session</h3>
          <ul>
            {runs.map((run) => (
              <li key={run.id}>
                <button
                  type="button"
                  className={`history-item${run.id === shown?.id ? " history-active" : ""}`}
                  aria-current={run.id === shown?.id}
                  onClick={() => setShownId(run.id)}
                >
                  <span className="history-question">{run.question}</span>
                  <span className="muted">{run.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
