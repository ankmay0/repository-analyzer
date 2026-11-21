# Repository Analyzer

Ask questions about a small set of interdependent repositories, and find out which of
them are affected when one fails.

You give it a few repository URLs. It clones them and describes them in one JSON file: a
graph of the repositories, their files and their symbols, and how everything connects
(which repository depends on which, what each file defines, and what imports what). It
also derives a repository-level dependency graph from the package manifests. Two things
are then available:

- **Impact analysis** — "if `markupsafe` breaks, what else is affected?" Answered
  deterministically from the dependency graph, without a language model.
- **Question answering** — a hub-and-spoke team of Claude agents, orchestrated with
  LangGraph. A coordinating agent (the hub) delegates to one expert agent per repository
  (the spokes) and answers from their reports, with `repo/path:line` citations.

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
│     knowledge/  outline (Python ast) ──► graph.json (files, symbols, imports)  │
│     manifests package.json / pyproject.toml / setup.cfg / requirements / go.mod│
│          └──► dependency_graph  edges between repositories, reverse BFS        │
│     (rag mode instead: chunker ──► retrieval/store, Chroma)                    │
│                                                                                │
│  GET /workspace/impact/{repo} ──► dependency_graph.impact_of   (no LLM)        │
│                                                                                │
│  POST /chat ──► agent/  hub and repository experts (see below)                 │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Agents: hub and spoke

```
                         user question
                               │
                        ┌──────▼──────┐   tools: list_repositories, analyze_impact,
                        │     HUB     │          find_usages, consult_repository,
                        │             │          show_graph
                        │ coordinator │   keeps the conversation; cannot read code
                        └──┬───┬───┬──┘
              ┌────────────┘   │   └────────────┐        consulted in parallel
       ┌──────▼─────┐   ┌──────▼─────┐   ┌──────▼─────┐
       │ markupsafe │   │   jinja    │   │   flask    │  tools: repo_map, find_symbol,
       │   expert   │   │   expert   │   │   expert   │         list_dependencies
       └────────────┘   └────────────┘   └────────────┘  each confined to one repository
```

- The **hub** sees the dependency graph and the conversation. It decides which experts to
  consult, writes each one a self-contained question, and composes the answer from their
  reports.
- An **expert** can search and read exactly one repository. It is stateless: it receives
  only the hub's question, runs its own search loop, and returns a cited report.
- Experts never communicate with each other. Everything passes through the hub.

Both are the same LangGraph loop (`agent/graph.py`: model node ⇄ tool node) with different
prompts, tools and state. For "if markupsafe breaks, what is affected and where?", the hub
calls `analyze_impact` and `find_usages`, consults the jinja and flask experts in the same
step for anything the graph does not show, and combines the results.

Backend layout (`backend/src/repo_analyzer/`):

| Module | Responsibility |
| --- | --- |
| `domain.py` | Immutable value types shared by every layer |
| `config.py` | All settings, overridable by `RA_*` environment variables |
| `ingestion/` | Fetch, scan, parse manifests, build the dependency graph, run indexing jobs |
| `knowledge/` | File outlines (Python `ast`), the graph builder, and its JSON store |
| `retrieval/` | Rag mode only: chunker's vector store and embedding backends |
| `agent/` | The hub (`hub.py`), repository experts (`experts.py`), their shared loop, tools and prompts, and the service that streams a turn |
| `api/` | Composition root (`services.py`), routes, request/response schemas |
| `workspace.py` | The current set of indexed repositories, persisted as JSON |

### Diagrams

The repository dependency graph is drawn with [React Flow](https://reactflow.dev) — pan,
zoom, rearrange — in two places:

- **The Dependencies panel** always shows it, and highlights the impact when you select a
  repository.
- **In chat**, when you ask to draw, visualise or map the repositories or a failure's
  impact, the backend emits the same diagram from the data (`requested_diagram`). It does
  not depend on the model choosing to draw: the live model reliably preferred inline ASCII,
  so the one picture the data defines exactly is produced from the data instead. Name a
  repository with a failure cue ("if markupsafe breaks") and it highlights the impact.

For any other picture — which files import a function, how modules relate — the hub can
call `show_graph` with nodes and arrows it supplies. That content is the model's summary,
not enforced, so only the dependency diagram is guaranteed exact.

A diagram travels as plain data (nodes, edges, a kind and status per node) in a `diagram`
event; the browser lays it out with dagre and renders it. No positions or styling come
from the model.

## What gets stored

Indexing writes one JSON file, `knowledge/graph.json`, derived from one scan of each
repository at its indexed commit. No model and no embeddings are involved, and the same
commits always produce a byte-identical file.

| Node | Fields |
| --- | --- |
| repository | name, commit |
| file | repo, path, language, module |
| symbol | repo, path, name, kind (class/function/variable), line — module-level only |

| Edge | Meaning |
| --- | --- |
| `depends_on` | repository → repository, from a package manifest |
| `defines` | file → the symbols it declares |
| `imports` | file → an indexed file it imports |
| `uses` | file → an indexed symbol it imports |

The graph connects repositories below the manifest level. `uses` edges are resolved from
import statements, following re-exports to the real definition, so `from flask import
Flask` links to the class in `flask/app.py`, not the `__init__.py` that re-exports it. For
the three Pallets repositories this takes about two seconds and yields roughly 1,500
symbols and 600 `uses` edges in one ~1 MB file.

The agents work entirely from this graph. The hub asks `find_usages` which files in other
repositories import a repository's code; each expert, confined to one repository, uses
`repo_map` (files and the names they define), `find_symbol` (where a name is defined and
who imports it), and `list_dependencies` (what one file imports from indexed code). There
is no file text in this mode, so the agents report structure and import links with exact
line numbers to cite, but cannot quote code or search its text.

Symbols and imports are extracted for **Python only**, using the standard library's `ast`
module, which reads code without running it. Files in other languages appear as file nodes
but contribute no symbols or import edges.

### The alternative: vector search

Setting `RA_KNOWLEDGE_MODE=rag` switches to an earlier design, kept so the two can be
compared on the same questions: files are split into line-range chunks, embedded, and
stored in Chroma, and experts search by meaning and can read file contents. In that mode
`graph.json` is not written, experts get `search_code`, `find_references` and `read_file`
in place of the graph tools, and the hub has no `find_usages`. An index built in one mode
cannot be queried in the other; the server says so and asks for a re-index.

## Design decisions

**Dependency edges come from manifests, not from the model.** "What breaks if X fails"
must be reproducible and auditable, so it is computed by graph traversal over declared
dependencies. The model's role is to explain the result and to locate where the dependency
is actually used.

**Hub and spoke rather than one agent with every tool.** An expert's search results stay
in that expert's context; the hub receives only the finished report. The hub's context
therefore grows with the number of consultations, not with the amount of code read, which
is what lets this scale past a few small repositories. Experts run in parallel, and a
hostile file in one repository can reach only that repository's expert directly. The cost
is more model calls: a cross-repository question is several conversations instead of one,
and a single-repository question pays for a hub turn on each side of the consultation.

**Isolation is enforced by construction, not by prompt.** An expert's tools are built for
one repository and take no repository argument, so there is no call an expert can make
that reads another repository. The hub has no tool that returns code.

**Agents plan their own retrieval.** Cross-repository questions need several hops: find
the dependents, then find where each one uses the dependency. The hub plans which experts
to ask; each expert plans its own searches. LangGraph runs the loops, keeps the hub's
per-conversation state, and enforces step limits on both.

**One expert failing does not lose the others' work.** A consultation that errors or runs
out of steps comes back to the hub as a report marked incomplete, alongside the reports
that succeeded.

**A single graph instead of embeddings by default.** The graph is exact, inspectable and
reproducible, and cross-repository usage becomes a lookup instead of a search. Two prices:
nothing matches by meaning (an expert finds `authenticate` for a question about "login" by
name, not by similarity), and nothing can quote code, because no file text is stored. The
graph answers where things are defined and how they are imported; for reading or
meaning-based search, use `RA_KNOWLEDGE_MODE=rag`.

**Only what can be resolved reliably is in the graph.** Imports and definitions are exact.
Function calls are not tracked, because without type information they cannot be resolved
in Python and wrong edges are worse than missing ones. A cross-repository import is linked
only when it resolves to exactly one indexed module that is a real package or a declared
dependency, so a stray `utils.py` elsewhere is not mistaken for the target.

**Dependency strength is tracked.** Runtime, optional and development dependencies are
distinguished. A repository reachable only through optional or dev edges is reported as a
weak impact. Manifests under `examples/`, `tests/` or `docs/` only ever yield dev edges.

**Repository content is untrusted input.** URLs are validated against a host allow-list
before `git` runs, with no shell and no option injection. The scanner indexes an allow-list
of file types, skips likely credential files and never follows symlinks, so a secret such
as `.env` never reaches the graph. In the default mode the agents read only `graph.json`,
never the checkout. The system prompt tells the model to
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

The default mode needs nothing beyond the clone. In `rag` mode the first indexing run
downloads the embedding model (about 70 MB) from Hugging Face; without that access, set
`RA_EMBEDDING_PROVIDER=hashing` to use the built-in lexical embedder, which needs no
download but matches shared words rather than meaning.

### Configuration

Set in the environment or in `backend/.env`. The full list is in
[`config.py`](backend/src/repo_analyzer/config.py).

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Required for chat. Indexing and impact analysis work without it. |
| `RA_LLM_MODEL` | `claude-opus-5-5` | Claude model used by the hub |
| `RA_LLM_EFFORT` | `medium` | Reasoning effort: `low`, `medium`, `high`, `xhigh`, `max` |
| `RA_EXPERT_MODEL` | same as hub | Model for repository experts |
| `RA_EXPERT_EFFORT` | same as hub | Reasoning effort for repository experts |
| `RA_MAX_PARALLEL_EXPERTS` | `4` | How many experts may work at the same time |
| `RA_KNOWLEDGE_MODE` | `json` | `json` (one graph file) or `rag` (vector search, reads code) |
| `RA_MAP_MAX_CHARS` | `60000` | Largest `repo_map` listing handed to a model at once |
| `RA_EMBEDDING_PROVIDER` | `fastembed` | Rag mode: `fastembed` (local semantic model) or `hashing` (lexical, offline) |
| `RA_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Rag mode: any text model supported by fastembed |
| `RA_DATA_DIR` | `.data` | Clones, knowledge files, vector index and workspace state |
| `RA_ALLOWED_GIT_HOSTS` | github.com, gitlab.com, bitbucket.org | Hosts repositories may be cloned from |
| `RA_MAX_REPOSITORIES` | `10` | Upper bound per workspace |

Changing `RA_KNOWLEDGE_MODE` requires re-indexing. In rag mode, changing the embedding
provider or model also requires deleting `RA_DATA_DIR/index`; the server refuses to open
an index built with a different embedder.

### API

| Method and path | Description |
| --- | --- |
| `GET /api/health` | Liveness, configured model and embedder |
| `GET /api/workspace` | Indexed repositories, dependency edges, indexing job status |
| `POST /api/workspace/index` | `{"repositories": [url, ...]}` — replaces the workspace; returns 202 |
| `GET /api/workspace/impact/{repo}` | Transitive dependents of `repo` with dependency chains |
| `POST /api/chat` | `{"thread_id", "message"}` — SSE stream: `token`, `tool_call` (hub step), `expert_step`, `diagram`, `sources`, `error`, `done` |

Interactive documentation is served at http://127.0.0.1:8000/docs.

## Tests

```bash
cd backend && ruff check . && mypy && pytest
cd frontend && npm run typecheck && npm test && npm run build
```

The backend suite runs offline. It indexes small fixture repositories in both modes and
drives the hub, the experts and the HTTP API with scripted chat models, so it
covers delegation, parallel consultations, repository isolation, streaming and error paths
without calling Anthropic.

## Limitations

- **Declared dependencies and static imports only.** Coupling that never appears in a
  manifest or an import statement (HTTP calls, shared databases, message queues, dynamic
  imports) is not in the graph, and nothing detects it systematically.
- **No code reading in the default mode.** The graph stores structure, not file contents,
  so the agents cite `file:line` but cannot quote the code, show signatures, or search its
  text. Switch to `RA_KNOWLEDGE_MODE=rag` for that.
- **Module-level symbols only.** The graph lists classes, functions and module-level
  variables, not methods or nested definitions.
- **Experts cannot see across repositories.** A finding that needs code from two
  repositories side by side depends on the hub asking each expert the right question and
  joining the reports. The hub and experts also share no memory between questions beyond
  the hub's own conversation history.
- **Cost.** A cross-repository question typically costs several times the tokens of a
  single-agent design. `RA_EXPERT_MODEL` and `RA_EXPERT_EFFORT` are the levers; a cheaper
  expert model has not been evaluated for answer quality here.
- **Python-only structure.** Symbols and import links exist only for Python files. Files in
  other languages appear as nodes but carry no symbols or edges.
- **No search by meaning in the default mode.** Vague or conceptual questions rely on
  matching names in the graph. `RA_KNOWLEDGE_MODE=rag` is there to compare.
- **Ecosystems:** npm, PyPI and Go modules. `setup.py` is not executed, so dependencies
  declared only there are missed. Adding an ecosystem is one parser function in
  `ingestion/manifests.py`.
- **Single process, single workspace, no authentication.** The server binds to loopback by
  default. The hub's conversation history is held in memory and is lost on restart; indexing state
  and the vector index persist on disk.
- **Indexing is a full rebuild** of every requested repository, and is not transactional
  across repositories. This is appropriate for a handful of small repositories.
- **Default branch only**, at the commit current when indexing ran.
