import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";

import { ApiError, streamChat, type Repository, type Source, type Usage, type Workspace } from "../api";
import { formatUsage } from "../usage";
import { Answer } from "./Answer";

interface ToolStep {
  name: string;
  args: Record<string, unknown>;
}

interface Turn {
  id: number;
  question: string;
  steps: ToolStep[];
  answer: string;
  sources: Source[];
  error: string | null;
  usage: Usage | null;
  pending: boolean;
}

const EXAMPLE_QUESTIONS = [
  "What does each repository declare as its dependencies?",
  "Which flask modules import from jinja2?",
  "Where does flask configure the Jinja environment?",
];

function newThreadId(): string {
  return crypto.randomUUID();
}

function sourceKey(source: Source): string {
  const lines =
    source.start_line === source.end_line
      ? `${source.start_line}`
      : `${source.start_line}-${source.end_line}`;
  return `${source.repo}/${source.path}:${lines}`;
}

function mergeSources(existing: Source[], incoming: Source[]): Source[] {
  const seen = new Set(existing.map(sourceKey));
  const merged = [...existing];
  for (const source of incoming) {
    if (!seen.has(sourceKey(source))) {
      seen.add(sourceKey(source));
      merged.push(source);
    }
  }
  return merged;
}

/** Link to the exact lines on GitHub when the repository and commit are known. */
function sourceUrl(source: Source, repositories: Repository[]): string | null {
  const repository = repositories.find((r) => r.name === source.repo);
  if (!repository?.commit || !repository.source.startsWith("https://github.com/")) return null;
  const base = repository.source.replace(/\.git$/, "");
  const path = source.path.split("/").map(encodeURIComponent).join("/");
  return `${base}/blob/${repository.commit}/${path}#L${source.start_line}-L${source.end_line}`;
}

function describeStep(step: ToolStep): string {
  const args = Object.entries(step.args)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
  return args ? `${step.name} (${args})` : step.name;
}

export function Chat({ workspace }: { workspace: Workspace | null }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [threadId, setThreadId] = useState(newThreadId);
  const abortRef = useRef<AbortController | null>(null);
  const nextTurnId = useRef(0);
  const endRef = useRef<HTMLDivElement | null>(null);

  const repositories = workspace?.repositories ?? [];
  const ready = repositories.length > 0;
  const busy = turns.some((turn) => turn.pending);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  // Stop any in-flight request when the component goes away.
  useEffect(() => () => abortRef.current?.abort(), []);

  async function ask(question: string) {
    const text = question.trim();
    if (!text || busy || !ready) return;
    setInput("");

    const id = nextTurnId.current++;
    const update = (change: (turn: Turn) => Turn) =>
      setTurns((current) => current.map((turn) => (turn.id === id ? change(turn) : turn)));
    setTurns((current) => [
      ...current,
      { id, question: text, steps: [], answer: "", sources: [], error: null, usage: null, pending: true },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      for await (const event of streamChat(threadId, text, controller.signal)) {
        switch (event.type) {
          case "token":
            update((turn) => ({ ...turn, answer: turn.answer + event.text }));
            break;
          case "tool_call":
            update((turn) => ({ ...turn, steps: [...turn.steps, event] }));
            break;
          case "sources":
            update((turn) => ({ ...turn, sources: mergeSources(turn.sources, event.sources) }));
            break;
          case "error":
            update((turn) => ({ ...turn, error: event.message }));
            break;
          case "usage":
            update((turn) => ({ ...turn, usage: event.usage }));
            break;
          case "done":
            break;
        }
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        const message =
          cause instanceof ApiError ? cause.message : "The connection to the server was lost.";
        update((turn) => ({ ...turn, error: message }));
      }
    } finally {
      update((turn) => ({ ...turn, pending: false }));
      if (abortRef.current === controller) abortRef.current = null;
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(input);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void ask(input);
    }
  }

  function startNewConversation() {
    abortRef.current?.abort();
    setTurns([]);
    setThreadId(newThreadId());
  }

  return (
    <div className="chat">
      <header className="chat-header">
        <h2>Ask about the repositories</h2>
        <button type="button" className="secondary" onClick={startNewConversation} disabled={turns.length === 0}>
          New conversation
        </button>
      </header>

      <div className="chat-turns" aria-live="polite">
        {turns.length === 0 && (
          <div className="empty">
            {ready ? (
              <>
                <p>Try one of these:</p>
                {EXAMPLE_QUESTIONS.map((question) => (
                  <button key={question} type="button" className="secondary" onClick={() => void ask(question)}>
                    {question}
                  </button>
                ))}
              </>
            ) : (
              <p>Index some repositories to start asking questions.</p>
            )}
          </div>
        )}

        {turns.map((turn) => (
          <article key={turn.id} className="turn">
            <p className="question">{turn.question}</p>

            {turn.steps.length > 0 && (
              <details className="steps" open={turn.pending}>
                <summary>
                  {turn.steps.length} research {turn.steps.length === 1 ? "step" : "steps"}
                </summary>
                <ol>
                  {turn.steps.map((step, index) => (
                    <li key={index}>{describeStep(step)}</li>
                  ))}
                </ol>
              </details>
            )}

            {turn.answer && (
              <div className="answer">
                <Answer text={turn.answer} pending={turn.pending} />
              </div>
            )}
            {turn.pending && !turn.answer && <p className="muted">Working…</p>}
            {turn.error && (
              <p className="notice notice-error" role="alert">
                {turn.error}
              </p>
            )}

            {turn.usage && (
              <p className="usage" title="Summed over every model call for this answer">
                {formatUsage(turn.usage)}
              </p>
            )}

            {!turn.pending && turn.sources.length > 0 && (
              <details className="sources">
                <summary>{turn.sources.length} sources consulted</summary>
                <ul>
                  {turn.sources.map((source) => {
                    const label = sourceKey(source);
                    const url = sourceUrl(source, repositories);
                    return (
                      <li key={label}>
                        {url ? (
                          <a href={url} target="_blank" rel="noreferrer">
                            {label}
                          </a>
                        ) : (
                          label
                        )}
                      </li>
                    );
                  })}
                </ul>
              </details>
            )}
          </article>
        ))}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={handleSubmit}>
        <label htmlFor="question" className="visually-hidden">
          Question
        </label>
        <textarea
          id="question"
          rows={2}
          placeholder={ready ? "Ask a question…" : "Index repositories first"}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={!ready}
          maxLength={8000}
        />
        {busy ? (
          <button type="button" onClick={() => abortRef.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="submit" disabled={!ready || !input.trim()}>
            Ask
          </button>
        )}
      </form>
    </div>
  );
}
