import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

import {
  ApiError,
  askDirect,
  type DirectAskResponse,
  type DirectToolCall,
  type Workspace,
} from "../api";
import { formatUsage } from "../usage";
import { Answer } from "./Answer";

function describeStep(step: DirectToolCall): string {
  const args = Object.entries(step.arguments)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
  return args ? `${step.name} (${args})` : step.name;
}

export function DirectPage({ workspace }: { workspace: Workspace | null }) {
  const repositories = workspace?.repositories ?? [];
  const [repo, setRepo] = useState("");
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DirectAskResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // If the field is still empty and something is already indexed, offer that name.
  useEffect(() => {
    const first = workspace?.repositories[0]?.name;
    if (first) setRepo((current) => (current.trim() === "" ? first : current));
  }, [workspace]);

  useEffect(() => () => abortRef.current?.abort(), []);

  async function ask() {
    const repoText = repo.trim();
    const questionText = question.trim();
    if (!repoText || !questionText || pending) return;

    const controller = new AbortController();
    abortRef.current = controller;
    setPending(true);
    setError(null);
    setResult(null);
    try {
      const response = await askDirect(repoText, questionText, controller.signal);
      setResult(response);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof ApiError ? cause.message : "The connection to the server was lost.");
      }
    } finally {
      setPending(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void ask();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void ask();
    }
  }

  const canAsk = repo.trim() !== "" && question.trim() !== "" && !pending;

  return (
    <div className="direct">
      <div className="page-intro">
        <h2>Direct analysis</h2>
        <p className="muted">
          Ask about one repository by reading its files directly. No Ctags JSON index or
          code graph is used. Paste a git URL, a local path, or the name of a checkout
          that is already on the server.
        </p>
      </div>

      <form className="direct-form" onSubmit={handleSubmit}>
        <label htmlFor="direct-repo">Repository</label>
        <input
          id="direct-repo"
          type="text"
          value={repo}
          placeholder="https://github.com/owner/repo, a local path, or a checkout name"
          onChange={(event) => setRepo(event.target.value)}
          disabled={pending}
          maxLength={2000}
          autoComplete="off"
        />
        {repositories.length > 0 && (
          <p className="direct-suggestions">
            Indexed:{" "}
            {repositories.map((item) => (
              <button
                key={item.name}
                type="button"
                className="secondary"
                disabled={pending}
                onClick={() => setRepo(item.name)}
              >
                {item.name}
              </button>
            ))}
          </p>
        )}

        <label htmlFor="direct-question">Question</label>
        <textarea
          id="direct-question"
          rows={3}
          value={question}
          placeholder="What should I look at first?"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={pending}
          maxLength={8000}
        />

        {pending ? (
          <button type="button" className="secondary" onClick={() => abortRef.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="submit" disabled={!canAsk}>
            Ask
          </button>
        )}
      </form>

      <DirectResult pending={pending} error={error} result={result} />
    </div>
  );
}

/** The loading line, error, or finished answer under the form. Exported for tests. */
export function DirectResult({
  pending,
  error,
  result,
}: {
  pending: boolean;
  error: string | null;
  result: DirectAskResponse | null;
}) {
  if (pending && !result) {
    return (
      <p className="muted" aria-live="polite">
        Working…
      </p>
    );
  }
  return (
    <div className="direct-result" aria-live="polite">
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      {result && (
        <>
          <p className="question">
            {result.question}
            {result.repo ? <span className="muted"> · {result.repo}</span> : null}
          </p>
          {result.tool_calls.length > 0 && (
            <details className="steps" open>
              <summary>
                {result.tool_calls.length} research{" "}
                {result.tool_calls.length === 1 ? "step" : "steps"}
              </summary>
              <ol>
                {result.tool_calls.map((step, index) => (
                  <li key={`${step.name}-${index}`}>{describeStep(step)}</li>
                ))}
              </ol>
            </details>
          )}
          {result.answer && (
            <div className="answer">
              <Answer text={result.answer} pending={false} />
            </div>
          )}
          {result.usage && (
            <p className="usage" title="Summed over every model call for this answer">
              {formatUsage(result.usage)}
            </p>
          )}
        </>
      )}
    </div>
  );
}
