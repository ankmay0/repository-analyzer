import type { ApproachStatus, BenchmarkQuestion, BenchmarkStatus } from "./types";

interface Props {
  status: BenchmarkStatus;
  questions: BenchmarkQuestion[];
  approaches: ReadonlySet<string>;
  selectedQuestions: ReadonlySet<string>;
  judge: boolean;
  githubRef: string;
  running: boolean;
  onToggleApproach: (name: string) => void;
  onToggleQuestion: (id: string) => void;
  onSelectQuestions: (ids: string[]) => void;
  onJudge: (value: boolean) => void;
  onGithubRef: (value: string) => void;
  onRun: () => void;
}

function ApproachOption({
  approach,
  checked,
  disabled,
  onToggle,
}: {
  approach: ApproachStatus;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  const unavailable = !approach.available;
  return (
    <li className={`approach-option${unavailable ? " approach-unavailable" : ""}`} data-approach={approach.name}>
      <label>
        <input
          type="checkbox"
          checked={checked && !unavailable}
          disabled={unavailable || disabled}
          onChange={onToggle}
        />
        <span className="approach-swatch" aria-hidden="true" />
        <strong>{approach.name}</strong>
      </label>
      <p className="muted">{approach.summary}</p>
      {unavailable && <p className="approach-reason">Unavailable: {approach.reason}</p>}
    </li>
  );
}

export function RunForm(props: Props) {
  const { status, questions, approaches, selectedQuestions, running } = props;
  const chosen = status.approaches.filter((a) => a.available && approaches.has(a.name));
  const selectedCount = questions.filter((q) => selectedQuestions.has(q.id)).length;
  const categories = [...new Set(questions.map((q) => q.category))];
  const blocked = !status.llm_configured
    ? (status.llm_message ?? "ANTHROPIC_API_KEY is not set on the server")
    : null;
  const canRun = blocked === null && !running && chosen.length > 0 && selectedCount > 0;
  const githubSelected = chosen.some((a) => a.name === "json-github");

  return (
    <form
      className="benchmark-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (canRun) props.onRun();
      }}
    >
      <fieldset disabled={running}>
        <legend>Approaches</legend>
        <ul className="approach-list">
          {status.approaches.map((approach) => (
            <ApproachOption
              key={approach.name}
              approach={approach}
              checked={approaches.has(approach.name)}
              disabled={running}
              onToggle={() => props.onToggleApproach(approach.name)}
            />
          ))}
        </ul>
      </fieldset>

      <details className="question-picker">
        <summary>
          Questions: {selectedCount} of {questions.length} selected
        </summary>
        <div className="question-actions">
          <button type="button" className="secondary" disabled={running} onClick={() => props.onSelectQuestions(questions.map((q) => q.id))}>
            All
          </button>
          <button type="button" className="secondary" disabled={running} onClick={() => props.onSelectQuestions([])}>
            None
          </button>
        </div>
        {categories.map((category) => (
          <fieldset key={category} disabled={running} className="question-group">
            <legend>{category}</legend>
            {questions
              .filter((q) => q.category === category)
              .map((q) => (
                <label key={q.id} className="question-option" title={q.question}>
                  <input
                    type="checkbox"
                    checked={selectedQuestions.has(q.id)}
                    onChange={() => props.onToggleQuestion(q.id)}
                  />
                  <span>{q.id}</span>
                </label>
              ))}
          </fieldset>
        ))}
      </details>

      <div className="benchmark-options">
        <label className="inline-check">
          <input
            type="checkbox"
            checked={props.judge}
            disabled={running}
            onChange={(event) => props.onJudge(event.target.checked)}
          />
          LLM judge (extra model call per answer)
        </label>
        {githubSelected && (
          <label className="github-ref">
            GitHub ref for json-github
            <input
              type="text"
              value={props.githubRef}
              disabled={running}
              onChange={(event) => props.onGithubRef(event.target.value)}
              spellCheck={false}
            />
          </label>
        )}
      </div>

      {blocked && (
        <p className="notice notice-error" role="alert">
          {blocked}
        </p>
      )}
      <button type="submit" disabled={!canRun}>
        {running ? "Running…" : `Run ${chosen.length} approach${chosen.length === 1 ? "" : "es"} × ${selectedCount} question${selectedCount === 1 ? "" : "s"}`}
      </button>
    </form>
  );
}
