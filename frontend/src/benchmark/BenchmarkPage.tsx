import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError } from "../api";
import {
  benchmarkCsvUrl,
  getBenchmarkQuestions,
  getBenchmarkRun,
  getBenchmarkStatus,
  startBenchmarkRun,
  streamBenchmarkEvents,
} from "./api";
import { CellDetail } from "./CellDetail";
import { applyEvent, isFinished, rowKey, summarize, toggleId, viewFromSnapshot, type RunView } from "./model";
import { ResultsGrid } from "./ResultsGrid";
import { RunForm } from "./RunForm";
import { SummaryCards } from "./SummaryCards";
import type { BenchmarkQuestion, BenchmarkStatus } from "./types";

const RUN_KEY = "benchmark-run-id";
const RETRY_MS = 1500;

function describe(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : "Cannot reach the API server.";
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => (clearTimeout(timer), resolve()), { once: true });
  });
}

/** Load the run, then follow its event stream until it ends; reconnect if the stream drops. */
async function followRun(
  runId: string,
  signal: AbortSignal,
  update: (change: (view: RunView | null) => RunView | null) => void,
  fail: (message: string) => void,
): Promise<void> {
  let after = 0;
  while (!signal.aborted) {
    try {
      if (after === 0) {
        const snapshot = await getBenchmarkRun(runId, signal);
        update(() => viewFromSnapshot(snapshot));
        after = snapshot.next_event;
        if (snapshot.state !== "running") return;
      }
      for await (const event of streamBenchmarkEvents(runId, after, signal)) {
        after += 1;
        update((view) => (view ? applyEvent(view, event) : view));
        if (event.type === "finished" || event.type === "failed") return;
      }
    } catch (cause) {
      if (signal.aborted) return;
      if (cause instanceof ApiError && cause.status === 404) {
        localStorage.removeItem(RUN_KEY);
        update(() => null);
        fail("That run no longer exists (the server was restarted).");
        return;
      }
      fail(describe(cause));
    }
    await sleep(RETRY_MS, signal);
  }
}

export function BenchmarkPage() {
  const [status, setStatus] = useState<BenchmarkStatus | null>(null);
  const [questions, setQuestions] = useState<BenchmarkQuestion[]>([]);
  const [approaches, setApproaches] = useState<ReadonlySet<string>>(new Set());
  const [selectedQuestions, setSelectedQuestions] = useState<ReadonlySet<string>>(new Set());
  const [judge, setJudge] = useState(false);
  const [githubRef, setGithubRef] = useState("main");
  const [run, setRun] = useState<RunView | null>(null);
  const [runId, setRunId] = useState<string | null>(() => localStorage.getItem(RUN_KEY));
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ approach: string; questionId: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([getBenchmarkStatus(controller.signal), getBenchmarkQuestions(controller.signal)])
      .then(([loaded, questionSet]) => {
        setStatus(loaded);
        setQuestions(questionSet.questions);
        setSelectedQuestions(new Set(questionSet.questions.map((q) => q.id)));
        setApproaches(new Set(loaded.approaches.filter((a) => a.available).map((a) => a.name)));
        setGithubRef(loaded.github_ref);
        // Pick up a run that is still going (for example after a page reload).
        setRunId((current) => current ?? loaded.running_run_id);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(describe(cause));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (runId === null) return;
    const controller = new AbortController();
    void followRun(runId, controller.signal, setRun, setError);
    return () => controller.abort();
  }, [runId]);

  const handleRun = useCallback(async () => {
    if (!status) return;
    setError(null);
    setSelected(null);
    try {
      const snapshot = await startBenchmarkRun({
        approaches: status.approaches.map((a) => a.name).filter((n) => approaches.has(n)),
        question_ids: questions.map((q) => q.id).filter((id) => selectedQuestions.has(id)),
        judge,
        github_ref: githubRef.trim() || undefined,
      });
      localStorage.setItem(RUN_KEY, snapshot.id);
      setRun(viewFromSnapshot(snapshot));
      setRunId(snapshot.id);
    } catch (cause) {
      setError(describe(cause));
    }
  }, [status, approaches, questions, selectedQuestions, judge, githubRef]);

  const running = run !== null && !isFinished(run);
  const runQuestions = useMemo(
    () => (run ? questions.filter((q) => run.questionIds.includes(q.id)) : []),
    [run, questions],
  );
  const summaries = useMemo(() => (run ? summarize(run.rows, run.approaches) : []), [run]);
  const selectedRow =
    run && selected
      ? run.rows.find((r) => rowKey(r.approach, r.question_id) === rowKey(selected.approach, selected.questionId))
      : undefined;
  const selectedQuestion = selected ? questions.find((q) => q.id === selected.questionId) : undefined;

  return (
    <div className="benchmark">
      <div className="benchmark-intro">
        <h2>Benchmark</h2>
        <p className="muted">
          Ask the known-answer questions about the test apps under each approach and compare correctness, tokens and
          latency.{status ? ` Model: ${status.llm_model}.` : ""}
        </p>
      </div>
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      {status ? (
        <RunForm
          status={status}
          questions={questions}
          approaches={approaches}
          selectedQuestions={selectedQuestions}
          judge={judge}
          githubRef={githubRef}
          running={running}
          onToggleApproach={(name) => setApproaches((current) => toggleId(current, name))}
          onToggleQuestion={(id) => setSelectedQuestions((current) => toggleId(current, id))}
          onSelectQuestions={(ids) => setSelectedQuestions(new Set(ids))}
          onJudge={setJudge}
          onGithubRef={setGithubRef}
          onRun={() => void handleRun()}
        />
      ) : (
        !error && <p className="muted">Loading…</p>
      )}

      {run && (
        <section className="benchmark-results" aria-label="Results">
          <div className="progress-row">
            <progress max={Math.max(run.progress.total, 1)} value={run.progress.completed} />
            <span className="muted" role="status">
              {run.progress.completed} of {run.progress.total} answers
              {run.state === "finished" && " · finished"}
              {run.state === "failed" && " · failed"}
              {running && run.current && ` · ${run.current.approach}${run.current.phase === "setup" ? " (preparing)" : run.current.question_id ? ` · ${run.current.question_id}` : ""}`}
            </span>
            <a
              className={`button secondary${run.rows.length === 0 ? " disabled" : ""}`}
              href={run.rows.length === 0 ? undefined : benchmarkCsvUrl(run.id)}
              download
              aria-disabled={run.rows.length === 0}
            >
              Download CSV
            </a>
          </div>
          {run.state === "failed" && run.error && (
            <p className="notice notice-error" role="alert">
              Run failed: {run.error}
            </p>
          )}
          {Object.entries(run.skipped).map(([name, reason]) => (
            <p key={name} className="notice">
              {name} was skipped: {reason}
            </p>
          ))}
          <SummaryCards summaries={summaries} />
          <ResultsGrid
            questions={runQuestions}
            approaches={run.approaches}
            rows={run.rows}
            skipped={run.skipped}
            current={run.current}
            selected={selected}
            onSelect={(approach, questionId) => setSelected({ approach, questionId })}
          />
          {selectedRow && selectedQuestion && (
            <CellDetail row={selectedRow} question={selectedQuestion} onClose={() => setSelected(null)} />
          )}
        </section>
      )}
    </div>
  );
}
