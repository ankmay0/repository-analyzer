import { type FormEvent, useState } from "react";

import type { Workspace } from "../api";

const EXAMPLE_REPOSITORIES = [
  "https://github.com/pallets/markupsafe",
  "https://github.com/pallets/jinja",
  "https://github.com/pallets/flask",
].join("\n");

interface Props {
  workspace: Workspace | null;
  onIndex: (repositories: string[]) => Promise<void>;
}

export function RepositoryPanel({ workspace, onIndex }: Props) {
  const [input, setInput] = useState(EXAMPLE_REPOSITORIES);
  const [submitting, setSubmitting] = useState(false);

  const job = workspace?.job;
  const running = job?.state === "running";
  const repositories = input
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await onIndex(repositories);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section>
      <h2>Repositories</h2>
      <form onSubmit={handleSubmit}>
        <label htmlFor="repositories">Repository URLs, one per line</label>
        <textarea
          id="repositories"
          rows={4}
          spellCheck={false}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          disabled={running}
        />
        <button type="submit" disabled={running || submitting || repositories.length === 0}>
          {running ? "Indexing…" : "Index repositories"}
        </button>
      </form>

      {running && (
        <p className="notice" role="status">
          {job.message}
        </p>
      )}
      {job?.state === "failed" && (
        <p className="notice notice-error" role="alert">
          Indexing failed: {job.error}
        </p>
      )}

      {workspace && workspace.repositories.length > 0 && (
        <ul className="repo-list">
          {workspace.repositories.map((repository) => (
            <li key={repository.name} title={repository.index_file}>
              <a href={repository.source} target="_blank" rel="noreferrer">
                {repository.name}
              </a>
              <span className="muted">
                {repository.file_count} files · {repository.symbol_count} symbols ·{" "}
                {repository.dependency_count} deps
                {repository.commit && <> · {repository.commit.slice(0, 7)}</>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
