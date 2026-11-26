# Benchmarks

Known-answer test apps and questions for comparing how Repository Analyzer answers.

- `fixtures/shop-api/`: FastAPI + PostgreSQL backend for customers, orders and fulfillment
  (migrations, pricing, an in-process cache, an external carrier API).
- `fixtures/shop-web/`: React + TypeScript front end that calls it.
- `questions.yaml`: 23 questions with a reference answer, key facts for automatic
  grading, and the files that prove each answer.

The fixtures are not meant to run in production; they exist so every answer can be
checked against code we control. Key-fact grading is case-insensitive substring
matching, and `a | b` means either spelling counts.

`backend/tests/test_benchmark_fixtures.py` checks that the questions file is well formed,
that every evidence file exists, and that both fixtures index cleanly.

## Running the benchmark

```bash
cd backend
pip install -e ".[dev,benchmark,embeddings]"   # embeddings: fastembed, a local ONNX model
export ANTHROPIC_API_KEY=...                    # required; nothing runs without it
python -m repo_analyzer.benchmark --dry-run     # check the setup, index the fixtures, no model calls
python -m repo_analyzer.benchmark               # all 23 questions x 5 approaches
python -m repo_analyzer.benchmark --approach tools-only --only order-pricing --judge
```

Output goes to `benchmarks/results/<timestamp>/` (git-ignored):

- `summary.md`: one row per approach (correct, mean key-fact score, LLM-judge result if
  `--judge`, mean tokens in/out, mean tool calls, mean latency, one-off setup tokens),
  then a question-by-approach grid;
- `results.csv`: one row per question and approach;
- `answers.jsonl`: the full answers, missing facts and tools used;
- `run.json`: model, settings, skipped approaches and the summary;
- `work/`: the fixture indexes, the endpoint metadata and the embeddings built for the run
  (the run's code graph lives in Neo4j and is deleted at the end).

### The approaches

Every approach uses the same model (`RA_LLM_MODEL`) and a fresh conversation per
question. `chat` is the Chat tab's agent: the same tools, skills and system prompt,
pointed at the local fixture checkouts. The other four use the same core rules (cite
`repo/path:line`, say when unsupported) and leave skills out so they are not given
instructions written for another approach's tools.

| Approach | Tools | Notes |
| --- | --- | --- |
| `chat` | `list_repositories`, `get_repository_info`, `search_code`, `read_file`, `load_skill` | The Chat tab's agent (same builder). `search_code` and `read_file` read the local checkout, so this approach can look at files beyond the JSON index. Always available. If `RA_ENDPOINT_METADATA` is on, `get_endpoint_metadata` is added too. |
| `json-metadata` | `list_repositories`, `get_repository_info`, `get_endpoint_metadata` | Before the questions, the model is asked once per repository to describe every endpoint and client call (data source, source types, tables, caching, transformations, side effects, external calls, location). The cost is reported as setup tokens. |
| `tools-only` | `list_repositories`, `list_files`, `search_code`, `read_file` | No JSON index. |
| `json-embeddings` | `list_repositories`, `get_repository_info`, `semantic_search` | Files are split into 60-line chunks (15 overlap) and embedded with `BAAI/bge-small-en-v1.5` via fastembed. The top `--top-k` (8) chunks are added to each question. Skipped if fastembed is not installed. |
| `json-graph` | `list_repositories`, `get_repository_info`, `trace_endpoint`, `callers_of`, `callees_of` | Before the questions, the fixtures are parsed into the Neo4j code graph (endpoint, functions, calls, tables) under the run's own namespace, which is deleted when the run ends. No code access: it can name the path and the tables but not what the code computes. Skipped unless `RA_GRAPH_ENABLED=true` and Neo4j is reachable. |

The JSON index and the file tools both use the local fixture folders. No GitHub
token is required.

### Grading

- **Key facts (always):** a question counts as correct when every key fact is present
  (case-insensitive; `a | b` means either). The summary also shows the mean share of
  facts found. This is strict and literal: a correct answer phrased differently can miss
  a fact, so read `answers.jsonl` before drawing conclusions.
- **LLM judge (`--judge`):** the same model compares each answer with the reference
  answer and returns correct or not, with a reason. Judge tokens are reported
  separately and not added to the approaches' totals.

Tokens are summed over every model call in the agent loop (the `usage` event from the
chat service). `input_tokens` includes prompt-cache reads and writes, which are also
shown separately in the CSV. Latency is wall-clock time per question.

### Without an API key

The command refuses to run without `ANTHROPIC_API_KEY` and writes nothing; it never
makes up results. `--dry-run` works offline. The test suite runs every approach end to
end with a fake model and a keyword embedder
(`backend/tests/test_benchmark.py`).

## From the web UI

Start the backend and the frontend as usual (see the main README) and open the
**Compare** tab. Ask a free-form question about the fixture apps (or an indexed
repository) and the five approaches answer side by side. It needs the same things
as the command line tool:

- `ANTHROPIC_API_KEY` in the backend environment, or the page shows an error and Compare
  is disabled;
- `pip install -e ".[embeddings]"` for `json-embeddings`;
- `pip install -e ".[graph]"`, `RA_GRAPH_ENABLED=true`, `NEO4J_PASSWORD` and a running
  Neo4j for `json-graph`.

Approaches that are missing something appear greyed out, with the reason (the same
information is at `GET /api/compare/status`). The backend must be started from a source
checkout, or with `RA_BENCHMARKS_DIR` pointing at a directory that holds `questions.yaml`
and `fixtures/`. Graded, known-answer runs of the question set still go through the
command line tool (`python -m repo_analyzer.benchmark`).
