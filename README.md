# Repository Analyzer

Ask questions about a small set of interdependent repositories, and find out which of
them are affected when one fails.

You give it a few repository URLs. It clones them, describes them in three JSON files (a
packed copy of the code, a map of what each file defines, and a graph of how everything
connects), and derives a dependency graph between them from their package manifests. Two
things are then available:

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
│     knowledge/  outline (Python ast) ──► pack.json, map.json, graph.json       │
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
                        │             │          show_dependency_graph, show_graph
                        │ coordinator │   keeps the conversation; cannot read code
                        └──┬───┬───┬──┘
              ┌────────────┘   │   └────────────┐        consulted in parallel
       ┌──────▼─────┐   ┌──────▼─────┐   ┌──────▼─────┐
       │ markupsafe │   │   jinja    │   │   flask    │  tools: repo_map, find_symbol,
       │   expert   │   │   expert   │   │   expert   │         find_references, read_file
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
| `knowledge/` | File outlines, the pack, map and graph builders, their JSON store, map rendering |
| `retrieval/` | Rag mode only: chunker's vector store and embedding backends |
| `agent/` | The hub (`hub.py`), repository experts (`experts.py`), their shared loop, tools and prompts, and the service that streams a turn |
| `api/` | Composition root (`services.py`), routes, request/response schemas |
| `workspace.py` | The current set of indexed repositories, persisted as JSON |

### Diagrams

When a question is about relationships, or the user asks to see or draw something, the hub
shows a diagram next to its written answer. Diagrams are drawn with
[React Flow](https://reactflow.dev) and can be panned, zoomed and rearranged.

- `show_dependency_graph` draws the repository dependency graph from the manifests,
  optionally highlighting a failing repository and everything it affects. It is exact.
- `show_graph` draws nodes and arrows the hub supplies, for anything else: which files
  import a function, how modules relate, the steps of a request. The hub is told to include
  only relationships its tools or the experts reported, but unlike the first tool this is
  not enforced, so treat such a diagram as the model's summary.

The backend sends a diagram as plain data (nodes, edges, a kind and a status per node) in
a `diagram` event. The browser lays it out with dagre and renders it; no positions or
styling come from the model. The Dependencies panel uses the same component.

## What gets stored

Indexing writes three JSON files, all derived from one scan of each repository at its
indexed commit. No model and no embeddings are involved, and the same commits always
produce byte-identical files.

| File | Contents | Used for |
| --- | --- | --- |
| `knowledge/<repo>/pack.json` | Every indexed file: path, language, size, token estimate and full text | Reading files and exact text search |
| `knowledge/<repo>/map.json` | Per file: classes, functions, methods with signatures, line ranges and the first docstring line; third-party imports | Finding where something lives without reading the code |
| `knowledge/graph.json` | Nodes for repositories, files and module-level symbols; edges `depends_on`, `defines`, `imports`, `uses` | "Which files in other repositories use this?" |

The graph is what connects repositories below the manifest level. `uses` edges are resolved
from import statements, following re-exports to the real definition, so
`from flask import Flask` links to the class in `flask/app.py` and not to the
`__init__.py` that re-exports it. For the three Pallets repositories this takes about two
seconds and yields roughly 1,500 symbols and 600 `uses` edges.

Agents never receive these files whole. The packed repositories alone are a few hundred
thousand tokens each, so the tools query them: the hub asks the graph where code is used,
and each expert reads its repository's map, then pulls specific files from the pack. The
map is rendered as an indented outline for the model and loses detail in steps (methods
and descriptions first) when a repository is too large for the budget.

Symbols and imports are extracted for **Python only**, using the standard library's `ast`
module, which reads code without running it. Files in other languages are packed and
listed in the map by name, and are covered by exact text search.

### The alternative: vector search

Setting `RA_KNOWLEDGE_MODE=rag` switches to the earlier design, kept so the two can be
compared on the same questions: files are split into line-range chunks, embedded, and
stored in Chroma, and experts search by meaning. In that mode none of the three files is
written, experts get `search_code` in place of `repo_map` and `find_symbol`, and the hub
has no `find_usages`. An index built in one mode cannot be queried in the other; the
server says so and asks for a re-index.

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

**Static files instead of embeddings by default.** The map and graph are exact, inspectable
and reproducible, and cross-repository usage becomes a lookup instead of a search. The
price is that nothing matches by meaning: an expert finds `authenticate` for a question
about "login" by reading the map, not by similarity. That works while a repository's map
fits in a prompt and gets weaker as repositories grow.

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
| `RA_EXPERT_MODEL` | same as hub | Model for repository experts, which do most of the reading |
| `RA_EXPERT_EFFORT` | same as hub | Reasoning effort for repository experts |
| `RA_MAX_PARALLEL_EXPERTS` | `4` | How many experts may work at the same time |
| `RA_KNOWLEDGE_MODE` | `json` | `json` (pack, map and graph files) or `rag` (vector search) |
| `RA_MAP_MAX_CHARS` | `60000` | Largest repository map handed to a model at once |
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

- **Declared dependencies only.** Coupling that never appears in a manifest (HTTP calls
  between services, shared databases, message queues) is not in the graph. The agent can
  look for evidence with the search tools, but nothing detects it systematically.
- **Repository-level granularity.** The graph says that flask depends on markupsafe, not
  which flask modules do. That second level is answered per question by the flask expert,
  not precomputed.
- **Experts cannot see across repositories.** A finding that needs code from two
  repositories side by side depends on the hub asking each expert the right question and
  joining the reports. The hub and experts also share no memory between questions beyond
  the hub's own conversation history.
- **Cost.** A cross-repository question typically costs several times the tokens of a
  single-agent design. `RA_EXPERT_MODEL` and `RA_EXPERT_EFFORT` are the levers; a cheaper
  expert model has not been evaluated for answer quality here.
- **Python-only structure.** Symbols and import links exist only for Python files. Dynamic
  imports, and anything reached by calling instead of importing, are not in the graph.
- **No search by meaning in the default mode.** Vague or conceptual questions rely on the
  expert reading the map well. `RA_KNOWLEDGE_MODE=rag` is there to compare.
- **Ecosystems:** npm, PyPI and Go modules. `setup.py` is not executed, so dependencies
  declared only there are missed. Adding an ecosystem is one parser function in
  `ingestion/manifests.py`.
- **Single process, single workspace, no authentication.** The server binds to loopback by
  default. The hub's conversation history is held in memory and is lost on restart; indexing state
  and the vector index persist on disk.
- **Indexing is a full rebuild** of every requested repository, and is not transactional
  across repositories. This is appropriate for a handful of small repositories.
- **Default branch only**, at the commit current when indexing ran.
