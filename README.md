# Repository Analyzer

Ask questions about a small set of repositories, answered from a plain JSON description
of each one and from the code itself.

You give it a few repository URLs. It clones them and writes **one JSON file per
repository** that describes it: basic info, the file tree, the dependencies declared in
package manifests, and the functions, classes and imports of each source file. A Claude
agent, orchestrated with LangGraph, then answers questions by reading those JSON files,
searching the code for exact text and reading files, and cites `repo/path:line`.

There is no vector database and no embedding model in the main flow. The JSON files are
readable by people and by other tools. Optionally (`RA_GRAPH_ENABLED=true`) each indexed
repository is also loaded into **Neo4j as a code graph** (endpoints, functions, calls and
the tables each function reads or writes) so a request can be traced from the router to
the database; the graph is rebuilt from the checkout on every run and the JSON stays the
source of truth. A one-page picture of the design is in
[`docs/architecture.html`](docs/architecture.html). Detailed backend docs are in
[`backend/docs/`](backend/docs/): [architecture](backend/docs/architecture.md),
[data models](backend/docs/datamodels.md) and [data flow](backend/docs/dataflow.md).

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
│     symbols     Universal Ctags: functions, classes, imports                   │
│     manifests   package.json, pyproject.toml, setup.cfg, requirements, go.mod  │
│     repo_index  one JSON file per repository: repos/<owner>__<name>.json       │
│     metadata    optional: one model call per repo, endpoint data-flow notes    │
│     graph_*     optional: ast → endpoints, calls, tables → Neo4j code graph    │
│                                                                                │
│  POST /chat ──► agent/   LangGraph:  START → agent ⇄ tools → END               │
│     tools: list_repositories, get_repository_info, search_code,                │
│            read_file, load_skill                                               │
│            + trace_endpoint, callers_of, callees_of (code graph, optional)     │
└────────────────────────────────────────────────────────────────────────────────┘
```

Backend layout (`backend/src/repo_analyzer/`):

| Module | Responsibility |
| --- | --- |
| `domain.py` | Immutable value types shared by every layer |
| `config.py` | All settings, overridable by `RA_*` environment variables |
| `ingestion/` | Fetch, scan, run Universal Ctags for symbols, parse manifests, write the JSON indexes, run indexing jobs; optional endpoint metadata and the Neo4j code graph (`graph_extract.py`, `graph_store.py`) |
| `retrieval/` | Optional in-memory embeddings for the benchmark's `json-embeddings` approach |
| `benchmark/` | The four answering approaches, the Compare service and the CLI benchmark |
| `agent/` | Tools (including `load_skill`), system prompt, skill loader, the LangGraph graph, and the service that streams a turn |
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
- `symbols` is present only for files where something was found. Functions, classes
  and methods come from [Universal Ctags](https://ctags.io/), run once per repository.
  Methods are named `Class.method`. `end_line` is included when Ctags provides it.
  Imports also come only from Ctags (`--extras=+r` reference tags). Coverage varies:
  Python is reasonably complete; JavaScript and TypeScript currently contribute
  almost none, and in that case the `imports` list is empty.
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

**Symbols come from Universal Ctags.** Indexing runs Ctags once over the checkout
(with `--output-format=json`) rather than shipping a parser per language. Only
functions, classes and methods are kept (methods as `Class.method`); variables,
constants, properties and symbols nested inside a function are dropped. Imports
are whatever Ctags reports as reference tags, which is language-dependent and
often empty for JavaScript and TypeScript. If Ctags is missing or fails, indexing
fails with a clear error; there is no fallback parser.

**An optional code graph for tracing, derived and disposable.** The JSON index says
where things are declared but not who calls whom, so tracing a request means many search
and read hops. With `RA_GRAPH_ENABLED=true` indexing also parses Python with `ast` and
loads endpoints, calls and table reads/writes into Neo4j. It is built last and a Neo4j
failure is only logged, so indexing never depends on it; with the flag off nothing
graph-related runs. The graph stores names, paths, line numbers, routes and table names,
never source text, and is queried only with fixed, parameterised Cypher.

**Repository content is untrusted input.** URLs are validated against a host allow-list
before `git` runs, with no shell and no option injection. The scanner indexes an allow-list
of file types, skips likely credential files and never follows symlinks. `search_code`
and `read_file` apply the same rules, and `read_file` cannot leave the checkout. The
system prompt tells the model to treat repository content as data, which reduces but does
not eliminate prompt injection from a hostile repository.

## Agent skills

The chat agent is given a short system prompt (its role, the untrusted-content
rule, how to cite `repo/path:line`, and brevity) plus a catalogue of **Agent
Skills**. Each skill is a folder of markdown that teaches it how to handle a
kind of question. When a skill fits, the model calls `load_skill` and follows
the instructions. The catalogue is built once at process start, so prompt
caching is unaffected.

Shipped skills live in [`backend/skills/`](backend/skills):

| Skill | When to use it |
| --- | --- |
| `repo-navigation` | Exploring a workspace, looking up files or symbols, searching or reading code |
| `answer-with-citations` | Any answer that states facts about the indexed repositories |
| `explain-architecture` | Architecture, structure, overview, or "how does this project work" |
| `trace-feature` | "Where is X handled", tracing a flow or feature across files; with the code graph on, traces an HTTP endpoint to the database with `trace_endpoint` first |
| `dependency-review` | Declared dependencies, hidden coupling, or "what breaks if X fails" |
| `draw-diagrams` | Answers that explain a flow, request path or architecture: draw a Mermaid flowchart instead of a README-style table |

### Diagrams in answers

When an answer explains a flow or how parts connect, the agent draws it as a
Mermaid flowchart in a ` ```mermaid ` code block (the base prompt and the
`draw-diagrams`, `trace-feature` and `explain-architecture` skills say so). The
chat draws these blocks with [Mermaid](https://mermaid.js.org/) once the answer
has finished streaming. A "Diagram source" toggle shows the code, and invalid
diagrams fall back to their source with the parse error. Mermaid is loaded
only the first time an answer contains a diagram, and runs with
`securityLevel: "strict"` because answers quote repository content.

### Adding a skill

1. Create `backend/skills/<skill-name>/SKILL.md`. The folder name must be
   lowercase letters, numbers and hyphens (no leading, trailing or doubled
   hyphen), and must match the `name` in the frontmatter.
2. Start the file with YAML frontmatter. `name` and `description` are required.
   The description should say **what** the skill does and **when** to use it, in
   the third person, and must not contain XML tags:
   ```markdown
   ---
   name: my-skill
   description: Does X. Use when the question is about Y.
   ---

   # My skill

   Step-by-step instructions for the agent...
   ```
3. Restart the server. Invalid files are skipped (and logged) rather than
   taking the process down. Point `RA_SKILLS_DIR` at another directory to load
   a different set. `GET /api/skills` lists the names and descriptions that
   were loaded.

## Running it

Requirements: Python 3.11+, Node 20+, `git`, [Universal Ctags](https://ctags.io/)
(for indexing), and an Anthropic API key for chat.

Install Universal Ctags if you do not already have it:

```bash
# Debian / Ubuntu
sudo apt install universal-ctags

# macOS
brew install universal-ctags
```

Check with `ctags --version` — it should say `Universal Ctags`. The path can be
overridden with `RA_CTAGS_PATH`.

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

On Windows (PowerShell), install Universal Ctags first (`winget install UniversalCtags.Ctags`
or `choco install universal-ctags`), then:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,benchmark]"
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python -m repo_analyzer     # serves http://127.0.0.1:8000

# in a second PowerShell window
cd frontend
npm install
npm run dev                 # http://localhost:5173
```

If `python -m repo_analyzer` fails with `No module named 'uvicorn'`, `python` is not the
venv's interpreter (for example MSYS2's Python is first on `PATH`). Start it from `backend`
with `.\.venv\Scripts\python.exe -m repo_analyzer` (or `uv run python -m repo_analyzer`).
In Git Bash, activate with `source .venv/Scripts/activate`.

Open http://localhost:5173, press **Index repositories** (the form is pre-filled with
`pallets/markupsafe`, `pallets/jinja` and `pallets/flask`), then ask a question. Indexing
takes seconds and downloads nothing but the repositories. The JSON files appear in
`backend/.data/repos/`.

To trace a problem, start the backend with
`python -m repo_analyzer --log-level debug --log-file .data/server.log`. The first lines
show the settings in effect (secrets only as set/missing), including whether the code
graph is enabled; Neo4j connection problems, per-repository graph syncs and one usage
line per chat answer follow. See
[backend/docs/architecture.md §13](backend/docs/architecture.md#13-running-locally).

Optional code graph: start Neo4j 5 (for example
`docker run -d -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/your-password neo4j:5`),
run `pip install -e ".[graph]"` in `backend`, set `RA_GRAPH_ENABLED=true` and
`NEO4J_PASSWORD=your-password` in `backend/.env`, restart and re-index. `/api/health`
then reports `graph_available: true`, and Neo4j Browser (http://localhost:7474) shows the
graph.

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
| `RA_CTAGS_PATH` | `ctags` | Path to the Universal Ctags binary. Indexing fails if it is missing. |
| `RA_SKILLS_DIR` | packaged `skills/` | Directory of Agent Skills (`<name>/SKILL.md`). Loaded once at startup. |
| `RA_BENCHMARKS_DIR` | `<checkout>/benchmarks` | Where the compare API and the CLI benchmark find `questions.yaml` and `fixtures/` |
| `RA_ENDPOINT_METADATA` | `false` | After indexing, ask the model once per repository for per-endpoint data-flow metadata and give the agent a `get_endpoint_metadata` tool |
| `RA_GRAPH_ENABLED` | `false` | After indexing, load each repository's code graph into Neo4j (needs `pip install -e ".[graph]"`) |
| `RA_NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt address |
| `RA_NEO4J_USER` | `neo4j` | Neo4j user |
| `RA_NEO4J_DATABASE` | `neo4j` | Neo4j database name |
| `NEO4J_PASSWORD` | — | Neo4j password (no `RA_` prefix, like `ANTHROPIC_API_KEY`) |

### API

| Method and path | Description |
| --- | --- |
| `GET /api/health` | Liveness, configured model, whether an API key is set (`llm_configured`; an empty key counts as not set), whether Universal Ctags is available, and `graph_available` (code graph enabled and Neo4j reachable) |
| `GET /api/workspace` | Indexed repositories (owner, branch, commit, file/symbol/dependency counts, JSON file name) and indexing job status |
| `POST /api/workspace/index` | `{"repositories": [url, ...]}` — replaces the workspace; returns 202 |
| `GET /api/skills` | Installed Agent Skills (name and description only) |
| `GET /api/compare/status` | Repositories you can compare on (the benchmark fixtures and every indexed repository) and each approach as available or not, with the reason |
| `POST /api/compare` | `{"question", "repository"?, "approaches"?}`: answers one question under every available approach at once, as one SSE stream. 503 without `ANTHROPIC_API_KEY`, 404 for an unknown repository |
| `POST /api/chat` | `{"thread_id", "message"}` — SSE stream: `token`, `tool_call`, `sources`, `error`, `usage`, `done` |

Interactive documentation is served at http://127.0.0.1:8000/docs.

### Token usage per answer

Every chat answer ends with a `usage` event just before `done`:

```json
{"input_tokens": 4400, "output_tokens": 95, "total_tokens": 4495,
 "cache_read_tokens": 2400, "cache_creation_tokens": 1000,
 "llm_calls": 3, "tool_calls": 2, "latency_ms": 5320}
```

Tokens are summed over every model call in the agent loop. `input_tokens` already
includes prompt-cache reads and writes; the two `cache_*` fields show how much.
`latency_ms` is wall-clock time for the whole answer. The same numbers are logged
by `repo_analyzer.agent.service` as one `chat usage ...` line per answer (also
attached to the log record as `chat_usage` for structured log handlers), and the
chat UI shows them under each answer. If the client disconnects mid-answer, no
usage event or log line is produced for that answer.

## MCP server

[`mcp_server.py`](backend/src/repo_analyzer/mcp_server.py) lets an AI client such as Cursor
or Claude Desktop use the analyzer as tools. It is a [FastMCP](https://gofastmcp.com) server
over stdio (the client starts it as a subprocess). It is one small file that calls the same
backend objects the web API uses, so there is no second copy of the logic.

| Tool | What it does |
| --- | --- |
| `index_repositories(repositories)` | Clones and indexes repositories (replaces the current set) and waits until done |
| `list_repositories()` | Lists the indexed repositories |
| `ask_question(question, thread_id)` | Asks the agent a question and returns its answer (needs `ANTHROPIC_API_KEY`) |
| `trace_endpoint(repo, method, path)` | Traces an endpoint to its database tables (needs the Neo4j graph, `RA_GRAPH_ENABLED=true`) |

It also has one resource, `info://status`, which reports whether the API key, Ctags and
the graph are ready.

Run it on Windows (PowerShell), from the `backend` folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp]"
copy .env.example .env      # then set ANTHROPIC_API_KEY
python -m repo_analyzer.mcp_server
```

Use `pip install -e ".[dev]"` instead if you also want to run the tests. To connect a client,
add this to Cursor's `.cursor/mcp.json` (or `claude_desktop_config.json` for Claude Desktop),
with your own paths. `cwd` is the `backend` folder, so the server finds `.env` and `.data`:

```json
{
  "mcpServers": {
    "repository-analyzer": {
      "command": "C:\\path\\to\\repository-analyzer\\backend\\.venv\\Scripts\\python.exe",
      "args": ["-m", "repo_analyzer.mcp_server"],
      "cwd": "C:\\path\\to\\repository-analyzer\\backend"
    }
  }
}
```

Then ask the client, for example: "Index https://github.com/pallets/flask, then ask how
routing works."

## Benchmark

`python -m repo_analyzer.benchmark` answers the known-answer questions in
[`benchmarks/questions.yaml`](benchmarks/questions.yaml) about the fixture apps in
`benchmarks/fixtures/`, under four approaches, and records correctness, tokens and
latency. See [`benchmarks/README.md`](benchmarks/README.md).

| Approach | What the agent gets |
| --- | --- |
| `chat` | The same agent as the Chat tab: Ctags JSON index, local file tools and skills |
| `json-metadata` | Ctags JSON index + per-endpoint metadata written by the model at index time; no code access |
| `tools-only` | No index: list, search and read the checkout |
| `json-embeddings` | Ctags JSON index + top-k similar code chunks from a local embeddings store; no code access |

## Tests

```bash
cd backend && ruff check . && mypy && pytest
cd frontend && npm run typecheck && npm test && npm run build
```

The backend suite runs offline (it does not call Anthropic or clone remote repositories).
Symbol tests feed Ctags JSON as canned lines, so they do not need the binary; one test
that runs real Ctags is skipped if it is not installed. Indexing the fixture
repositories in the other tests does require Universal Ctags. The agent and the HTTP
API are driven with a scripted chat model. Skill tests cover parsing, invalid
frontmatter, the catalogue in the system prompt, and the `load_skill` tool. Code-graph
tests run the extractor on the `shop-api` fixture and use an in-memory `FakeGraphStore`,
so no Neo4j is needed; the one Neo4j round-trip test runs only when `RA_TEST_NEO4J_URI`
(and `RA_TEST_NEO4J_PASSWORD`) are set.

## Limitations

- **Exact search only.** Without embeddings, `search_code` finds what the code literally
  says. A question like "where is authentication handled?" works less well when the code
  never uses that word; the agent has to guess likely spellings and browse the file tree.
- **No cross-repository analysis.** There is no cross-repository dependency graph and no
  "what breaks if X fails" answer. The JSON lists each repository's declared dependencies, and the agent can
  reason from those and from usages it finds, but nothing computes impact.
- **Symbols are only as complete as Ctags.** Arrow functions assigned to `const` are
  usually tagged as constants and therefore dropped. TypeScript class methods in `.tsx`
  files are often missing in current Universal Ctags. Import lists are frequently empty
  for JavaScript and TypeScript. Variables, type aliases and enums are not recorded.
  Install Universal Ctags; indexing will not start without it.
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
- **Code graph coverage** (optional feature): Python only, FastAPI routes only
  (`include_router(..., prefix=...)` prefixes are not applied), calls resolved by name so
  dynamic dispatch and dependency injection show up as gaps, and tables are linked within
  one repository only.

### Compare tab

The **Compare** tab answers one free-form question under all four approaches at the
same time and shows the answers side by side. Pick a repository (an indexed one, or the
benchmark fixtures), type a question and press Compare. Each column streams its own
answer, with a footer showing total, input, output and cached tokens, latency and tool
calls; the column with the fewest tokens and the fastest one are marked. An approach
that cannot run (for example `json-embeddings` without fastembed) appears greyed
out with the reason. On narrow screens the columns stack. The
questions asked in the session are listed underneath and can be reopened.

`POST /api/compare` streams events that all carry `approach`, except `plan` (the
approaches and whether each can run) and the final `done`: `setup`, `started`, `token`,
`tool_call`, `error`, `unavailable`, then `usage` (input, output, cache tokens, latency,
tool calls, plus the one-off setup tokens for endpoint metadata or embeddings the first
time) and `approach_done`. The other three approaches share the benchmark's tools, prompts, metadata and
embedding setup. The `chat` column is the Chat tab's agent (same builder, system
prompt, skills and tools), pointed at the same local clone or fixtures. Metadata and embeddings are prepared once per
repository and reused for the next question; for an indexed repository the agent only
sees that repository. A compare run makes up to four agent runs for one question, so it
costs about four times a chat answer.
