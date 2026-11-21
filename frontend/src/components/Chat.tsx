import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";

import {
  ApiError,
  streamChat,
  type Diagram,
  type Repository,
  type Source,
  type Workspace,
} from "../api";
import { GraphView } from "./GraphView";

/** One research action, attributed to the agent that took it. */
interface Step {
  actor: string; // "Coordinator" (the hub) or "<repository> expert"
  text: string;
}

const COORDINATOR = "Coordinator";

interface Turn {
  id: number;
  question: string;
  steps: Step[];
  answer: string;
  diagrams: Diagram[];
  sources: Source[];
  error: string | null;
  pending: boolean;
}

const EXAMPLE_QUESTIONS = [
  "Draw how these repositories depend on each other.",
  "If markupsafe breaks, which repositories are affected and where do they use it?",
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

function describeCall(name: string, args: Record<string, unknown>): string {
  const rendered = Object.entries(args)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
  return rendered ? `${name} (${rendered})` : name;
}

function coordinatorStep(name: string, args: Record<string, unknown>): Step {
  if (name === "consult_repository") {
    return { actor: COORDINATOR, text: `ask the ${String(args.repo)} expert: ${String(args.question)}` };
  }
  return { actor: COORDINATOR, text: describeCall(name, args) };
}

/** Group steps by agent, keeping agents in the order they first acted. */
function groupByActor(steps: Step[]): [string, string[]][] {
  const groups = new Map<string, string[]>();
  for (const step of steps) {
    const texts = groups.get(step.actor) ?? [];
    texts.push(step.text);
    groups.set(step.actor, texts);
  }
  return [...groups.entries()];
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
      { id, question: text, steps: [], answer: "", diagrams: [], sources: [], error: null, pending: true },
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
            update((turn) => ({
              ...turn,
              steps: [...turn.steps, coordinatorStep(event.name, event.args)],
            }));
            break;
          case "expert_step":
            update((turn) => ({
              ...turn,
              steps: [
                ...turn.steps,
                { actor: `${event.repo} expert`, text: describeCall(event.name, event.args) },
              ],
            }));
            break;
          case "diagram":
            update((turn) => ({ ...turn, diagrams: [...turn.diagrams, event.diagram] }));
            break;
          case "sources":
            update((turn) => ({ ...turn, sources: mergeSources(turn.sources, event.sources) }));
            break;
          case "error":
            update((turn) => ({ ...turn, error: event.message }));
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
                {groupByActor(turn.steps).map(([actor, texts]) => (
                  <div key={actor} className="step-group">
                    <strong>{actor}</strong>
                    <ol>
                      {texts.map((text, index) => (
                        <li key={index}>{text}</li>
                      ))}
                    </ol>
                  </div>
                ))}
              </details>
            )}

            {turn.answer && (
              <div className="answer">
                <Markdown>{turn.answer}</Markdown>
              </div>
            )}
            {turn.diagrams.map((diagram, index) => (
              <figure key={index} className="diagram">
                <figcaption>{diagram.title}</figcaption>
                <GraphView diagram={diagram} direction="LR" />
              </figure>
            ))}
            {turn.pending && !turn.answer && <p className="muted">Working…</p>}
            {turn.error && (
              <p className="notice notice-error" role="alert">
                {turn.error}
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
