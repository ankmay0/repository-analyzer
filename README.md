# Repository Analyzer

Ask questions about a small set of interdependent repositories, and find out which of
them are affected when one fails.

You give it a few repository URLs. It clones them, indexes the code for retrieval, and
derives a dependency graph between them from their package manifests. Two things are then
available:

- **Impact analysis** — "if `markupsafe` breaks, what else is affected?" Answered
  deterministically from the dependency graph, without a language model.
- **Question answering** — a Claude agent, orchestrated with LangGraph, that retrieves
  from the indexed code and answers with `repo/path:line` citations.

## Architecture

```
                 ┌──────────────── React UI (Vite) ────────────────┐
                 │ repositories · dependency graph/impact · chat   │
                 └───────────────────────┬─────────────────────────┘
                                         │ HTTP + server-sent events
┌────────────────────────────────────────┴───────────────────────────────────────┐
│ FastAPI (api/)                                                                 │
│                                                                                │
│  POST /workspace/index ──► ingestion/                                          │
│     fetcher   validate URL, shallow git clone                                  │
│     scanner   allow-listed text files only; no secrets, lockfiles, symlinks    │
│     chunker   overlapping line windows ──► retrieval/store (Chroma, cosine)    │
│     manifests package.json / pyproject.toml / setup.cfg / requirements / go.mod│
│          └──► dependency_graph  edges between repositories, reverse BFS        │
│                                                                                │
│  GET /workspace/impact/{repo} ──► dependency_graph.impact_of   (no LLM)        │
│                                                                                │
│  POST /chat ──► agent/  LangGraph:  START → agent ⇄ tools → END                │
│                 tools: list_repositories, analyze_impact, search_code,         │
│                        find_references, read_file                              │
└────────────────────────────────────────────────────────────────────────────────┘
```

Backend layout (`backend/src/repo_analyzer/`):

| Module | Responsibility |
| --- | --- |
| `domain.py` | Immutable value types shared by every layer |
| `config.py` | All settings, overridable by `RA_*` environment variables |
| `ingestion/` | Fetch, scan, chunk, parse manifests, build the dependency graph, run indexing jobs |
| `retrieval/` | Embedding backends behind one protocol; the Chroma vector store |
| `agent/` | Tools, system prompt, the LangGraph graph, and the service that streams a turn |
| `api/` | Composition root (`services.py`), routes, request/response schemas |
| `workspace.py` | The current set of indexed repositories, persisted as JSON |

## Design decisions

**Dependency edges come from manifests, not from the model.** "What breaks if X fails"
must be reproducible and auditable, so it is computed by graph traversal over declared
dependencies. The model's role is to explain the result and to locate where the dependency
is actually used.

**Agentic retrieval rather than a fixed retrieve-then-generate pipeline.** Cross-repository
questions need several hops: find the dependents, then find where each one uses the
dependency. The model plans those hops by choosing among the tools; LangGraph runs the
loop, keeps per-conversation state, and enforces a step limit.

**Two search tools.** Vector search finds code by meaning. Exact text search finds
identifiers and import statements, which is what impact questions usually need and what
embeddings are weakest at.

**Line-based chunks.** Every chunk is an exact `path:start-end` range, so citations are
verifiable and link straight to the lines on GitHub. Chunks prefer to end on a blank line.
AST-aware chunking would cut fewer functions in half, at the cost of a parser per language.

**Dependency strength is tracked.** Runtime, optional and development dependencies are
distinguished. A repository reachable only through optional or dev edges is reported as a
weak impact. Manifests under `examples/`, `tests/` or `docs/` only ever yield dev edges.

**Repository content is untrusted input.** URLs are validated against a host allow-list
before `git` runs, with no shell and no option injection. The scanner indexes an allow-list
of file types, skips likely credential files and never follows symlinks. `read_file`
applies the same rules and cannot leave the checkout. The system prompt tells the model to
treat retrieved content as data, which reduces but does not eliminate prompt injection
from a hostile repository.

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
`pallets/markupsafe`, `pallets/jinja` and `pallets/flask`, where flask depends on jinja and
both depend on markupsafe), then ask a question or pick a repository under
**Dependencies**.

The first indexing run downloads the embedding model (about 70 MB) from Hugging Face.
Without that access, set `RA_EMBEDDING_PROVIDER=hashing` to use the built-in lexical
embedder, which needs no download but matches shared words rather than meaning.

### Configuration

Set in the environment or in `backend/.env`. The full list is in
[`config.py`](backend/src/repo_analyzer/config.py).

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required for chat. Indexing and impact analysis work without it. |
| `RA_LLM_MODEL` | `claude-opus-5-5` | Claude model used by the agent |
| `RA_LLM_EFFORT` | `medium` | Reasoning effort: `low`, `medium`, `high`, `xhigh`, `max` |
| `RA_EMBEDDING_PROVIDER` | `fastembed` | `fastembed` (local semantic model) or `hashing` (lexical, offline) |
| `RA_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Any text model supported by fastembed |
| `RA_DATA_DIR` | `.data` | Clones, vector index and workspace state |
| `RA_ALLOWED_GIT_HOSTS` | github.com, gitlab.com, bitbucket.org | Hosts repositories may be cloned from |
| `RA_MAX_REPOSITORIES` | `10` | Upper bound per workspace |

Changing the embedding provider or model requires deleting `RA_DATA_DIR/index` and
re-indexing; the server refuses to open an index built with a different embedder.

### API

| Method and path | Description |
| --- | --- |
| `GET /api/health` | Liveness, configured model and embedder |
| `GET /api/workspace` | Indexed repositories, dependency edges, indexing job status |
| `POST /api/workspace/index` | `{"repositories": [url, ...]}` — replaces the workspace; returns 202 |
| `GET /api/workspace/impact/{repo}` | Transitive dependents of `repo` with dependency chains |
| `POST /api/chat` | `{"thread_id", "message"}` — SSE stream: `token`, `tool_call`, `sources`, `error`, `done` |

Interactive documentation is served at http://127.0.0.1:8000/docs.

## Tests

```bash
cd backend && ruff check . && mypy && pytest
cd frontend && npm run typecheck && npm test && npm run build
```

The backend suite runs offline. It indexes small fixture repositories with the hashing
embedder and drives the agent and the HTTP API with a scripted chat model, so it covers the
graph, tool execution, streaming and error paths without calling Anthropic.

## Limitations

- **Declared dependencies only.** Coupling that never appears in a manifest (HTTP calls
  between services, shared databases, message queues) is not in the graph. The agent can
  look for evidence with the search tools, but nothing detects it systematically.
- **Repository-level granularity.** The graph says that flask depends on markupsafe, not
  which flask modules do. That second level is answered per question by the agent, not
  precomputed.
- **Ecosystems:** npm, PyPI and Go modules. `setup.py` is not executed, so dependencies
  declared only there are missed. Adding an ecosystem is one parser function in
  `ingestion/manifests.py`.
- **Single process, single workspace, no authentication.** The server binds to loopback by
  default. Conversation history is held in memory and is lost on restart; indexing state
  and the vector index persist on disk.
- **Indexing is a full rebuild** of every requested repository, and is not transactional
  across repositories. This is appropriate for a handful of small repositories.
- **Default branch only**, at the commit current when indexing ran.
