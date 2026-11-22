# Repository Analyzer

Ask questions about a small set of repositories, answered from a plain JSON description
of each one and from the code itself.

You give it a few repository URLs. It clones them and writes **one JSON file per
repository** that describes it: basic info, the file tree, the dependencies declared in
package manifests, and the functions, classes and imports of each source file. A Claude
agent, orchestrated with LangGraph, then answers questions by reading those JSON files,
searching the code for exact text and reading files, and cites `repo/path:line`.

There is no vector database, no embedding model and no dependency graph. The JSON files
are readable by people and by other tools. A one-page picture of the design is in
[`docs/architecture.html`](docs/architecture.html).

## Architecture

```
                 ┌──────────────── React UI (Vite) ────────────────┐
                 │ repositories · chat                              │
                 └───────────────────────┬─────────────────────────┘
                                         │ HTTP + server-sent events
┌────────────────────────────────────────┴───────────────────────────────────────┐
│ FastAPI (api/)                                                                 │
│                                                                                │
│  POST /workspace/index ──► ingestion/                                          │
│     fetcher     validate URL, shallow git clone                                │
│     scanner     allow-listed text files only; no secrets, lockfiles, symlinks  │
│     symbols     functions / classes / imports (Python: ast; others: regexes)   │
│     manifests   package.json, pyproject.toml, setup.cfg, requirements, go.mod  │
│     repo_index  one JSON file per repository: repos/<owner>__<name>.json       │
│                                                                                │
│  POST /chat ──► agent/   LangGraph:  START → agent ⇄ tools → END               │
│     tools: list_repositories, get_repository_info, search_code, read_file      │
└────────────────────────────────────────────────────────────────────────────────┘
```

Backend layout (`backend/src/repo_analyzer/`):

| Module | Responsibility |
| --- | --- |
| `domain.py` | Immutable value types shared by every layer |
| `config.py` | All settings, overridable by `RA_*` environment variables |
| `ingestion/` | Fetch, scan, extract symbols, parse manifests, write the JSON indexes, run indexing jobs |
| `agent/` | Tools, system prompt, the LangGraph graph, and the service that streams a turn |
| `api/` | Composition root (`services.py`), routes, request/response schemas |
| `workspace.py` | The current set of indexed repositories, persisted as `workspace.json` |

### The JSON index

Each repository is described by `<RA_DATA_DIR>/repos/<owner>__<name>.json` (`local` is the
owner for a local directory). Abridged example:

```json
{
  "schema_version": 1,
  "name": "flask",
  "owner": "pallets",
  "url": "https://github.com/pallets/flask",
  "default_branch": "main",
  "commit": "3f2a9c1d8e0b4c5a6f7e8d9c0b1a2f3e4d5c6b7a",
  "indexed_at": "2026-11-22T11:30:00+00:00",
  "stats": {
    "files": 214,
    "lines": 31870,
    "size_bytes": 1084312,
    "languages": { "python": 96, "markdown": 41, "toml": 2 }
  },
  "manifests": [{ "path": "pyproject.toml", "ecosystem": "pypi", "package": "flask" }],
  "dependencies": [
    { "name": "jinja2", "ecosystem": "pypi", "kind": "runtime", "manifest": "pyproject.toml" },
    { "name": "pytest", "ecosystem": "pypi", "kind": "dev", "manifest": "pyproject.toml" }
  ],
  "files": [
    {
      "path": "src/flask/app.py",
      "language": "python",
      "size_bytes": 48210,
      "line_count": 1371,
      "symbols": {
        "functions": [{ "name": "Flask.run", "line": 612, "end_line": 702 }],
        "classes": [{ "name": "Flask", "line": 108, "end_line": 1371 }],
        "imports": [{ "module": "os", "line": 1 }, { "module": "jinja2", "line": 14 }]
      }
    }
  ]
}
```

- `dependencies[].kind` is `runtime`, `optional` or `dev`. Manifests under `examples/`,
  `tests/` or `docs/` only ever yield `dev`.
- `symbols` is present only for files where something was found. Python is parsed with
  `ast` (methods are named `Class.method`, and `end_line` is known); JavaScript,
  TypeScript, Go, Rust, Java, Kotlin, C#, Ruby, PHP, shell, C/C++ and protobuf use simple
  regular expressions and give only a start line.
- The JSON holds no file contents and no model-written summaries.

## Design decisions

**Plain JSON instead of embeddings and a graph.** The goal is a small, inspectable
system. Indexing needs no model download and no database, produces files you can open in
an editor, and is deterministic. The earlier design (embeddings, a Chroma store, a
dependency graph and an impact-analysis endpoint) is gone.

**Agentic lookups rather than a fixed retrieve-then-generate pipeline.** Questions often
need several hops: look at a repository's structure, search for a name, read the lines.
The model plans those hops by choosing among the tools; LangGraph runs the loop, keeps
per-conversation state, and enforces a step limit.

**Structure from the JSON, code from the files.** `get_repository_info` answers "what is
in this repository, what does it declare, where is X defined"; `search_code` (exact text
or regular expression) and `read_file` answer "where is X used" and "how does it work".
The JSON stays small because it never copies file contents.

**Symbols without parser dependencies.** Python uses the standard library `ast`; other
languages use per-line regexes. They are right for ordinary code and wrong for unusual
formatting, so the tool descriptions tell the model they are heuristics.

**Repository content is untrusted input.** URLs are validated against a host allow-list
before `git` runs, with no shell and no option injection. The scanner indexes an allow-list
of file types, skips likely credential files and never follows symlinks. `search_code`
and `read_file` apply the same rules, and `read_file` cannot leave the checkout. The
system prompt tells the model to treat repository content as data, which reduces but does
not eliminate prompt injection from a hostile repository.

## Running it

Requirements: Python 3.11+, Node 20+, `git`, and an Anthropic API key for chat.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # then set ANTHROPIC_API_KEY
python -m repo_analyzer     # serves http://127.0.0.1:8000
```

```bash
cd frontend
npm install
npm run dev                 # serves http://localhost:5173, proxies /api to the backend
```

Open http://localhost:5173, press **Index repositories** (the form is pre-filled with
`pallets/markupsafe`, `pallets/jinja` and `pallets/flask`), then ask a question. Indexing
takes seconds and downloads nothing but the repositories. The JSON files appear in
`backend/.data/repos/`.

Upgrading from the embeddings version: delete `RA_DATA_DIR/index` and the old clone
folders in `RA_DATA_DIR/repos/` (clones now live in `RA_DATA_DIR/checkouts/`), then
index again. The old `workspace.json` is ignored.

### Configuration

Set in the environment or in `backend/.env`. The full list is in
[`config.py`](backend/src/repo_analyzer/config.py).

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required for chat. Indexing works without it. |
| `RA_LLM_MODEL` | `claude-opus-5-5` | Claude model used by the agent |
| `RA_LLM_EFFORT` | `medium` | Reasoning effort: `low`, `medium`, `high`, `xhigh`, `max` |
| `RA_DATA_DIR` | `.data` | `checkouts/` (clones), `repos/` (JSON indexes) and `workspace.json` |
| `RA_ALLOWED_GIT_HOSTS` | github.com, gitlab.com, bitbucket.org | Hosts repositories may be cloned from |
| `RA_MAX_REPOSITORIES` | `10` | Upper bound per workspace |
| `RA_MAX_FILE_BYTES` | `200000` | Larger files are not indexed, searched or readable |

### API

| Method and path | Description |
| --- | --- |
| `GET /api/health` | Liveness and configured model |
| `GET /api/workspace` | Indexed repositories (owner, branch, commit, file/symbol/dependency counts, JSON file name) and indexing job status |
| `POST /api/workspace/index` | `{"repositories": [url, ...]}` — replaces the workspace; returns 202 |
| `POST /api/chat` | `{"thread_id", "message"}` — SSE stream: `token`, `tool_call`, `sources`, `error`, `done` |

Interactive documentation is served at http://127.0.0.1:8000/docs.

## Tests

```bash
cd backend && ruff check . && mypy && pytest
cd frontend && npm run typecheck && npm test && npm run build
```

The backend suite runs offline. It indexes small fixture repositories and checks the JSON
that comes out (file tree, dependencies, symbols, secrets excluded), and it drives the
tools, the agent and the HTTP API with a scripted chat model, so it covers tool
execution, streaming and error paths without calling Anthropic.

## Limitations

- **Exact search only.** Without embeddings, `search_code` finds what the code literally
  says. A question like "where is authentication handled?" works less well when the code
  never uses that word; the agent has to guess likely spellings and browse the file tree.
- **No cross-repository analysis.** There is no dependency graph and no "what breaks if X
  fails" answer. The JSON lists each repository's declared dependencies, and the agent can
  reason from those and from usages it finds, but nothing computes impact.
- **Heuristic symbols outside Python.** Regex extraction misses unusual formatting and can
  be fooled by code in strings or comments; it records no end lines. Other file types
  (JSON, YAML, Markdown, ...) have no symbols.
- **Large repositories.** The index is complete, but a single `get_repository_info`
  result is capped at about 40 000 characters; beyond that it drops symbols and then
  files, and says so, so the agent has to ask for one directory at a time with `path`.
  Regular expressions given to `search_code` run on the server, so very large repositories
  with pathological patterns can be slow (patterns are capped at 300 characters).
- **Ecosystems:** npm, PyPI and Go modules. `setup.py` is not executed, so dependencies
  declared only there are missed. Adding an ecosystem is one parser function in
  `ingestion/manifests.py`.
- **Single process, single workspace, no authentication.** The server binds to loopback by
  default. Conversation history is held in memory and is lost on restart; the JSON
  indexes and workspace state persist on disk.
- **Indexing is a full rebuild** of every requested repository. The JSON files are written
  only after every repository has been fetched and scanned, so a failed run keeps the
  previous indexes, though checkouts fetched before the failure are already refreshed.
- **Default branch only**, at the commit current when indexing ran.
