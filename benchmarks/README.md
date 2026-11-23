# Benchmarks

Known-answer test apps and questions for comparing how Repository Analyzer answers.

- `fixtures/shop-api/`: FastAPI + PostgreSQL backend for customers, orders and fulfillment
  (migrations, pricing, an in-process cache, an external carrier API).
- `fixtures/shop-web/`: React + TypeScript front end that calls it.
- `questions.yaml`: 19 questions with a reference answer, key facts for automatic
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
export GITHUB_TOKEN=...                         # optional; read-only is enough
python -m repo_analyzer.benchmark --dry-run     # check the setup, index the fixtures, no model calls
python -m repo_analyzer.benchmark               # all 19 questions x 4 approaches
python -m repo_analyzer.benchmark --approach tools-only --only order-pricing --judge
```

Output goes to `benchmarks/results/<timestamp>/` (git-ignored):

- `summary.md`: one row per approach (correct, mean key-fact score, LLM-judge result if
  `--judge`, mean tokens in/out, mean tool calls, mean latency, one-off setup tokens),
  then a question-by-approach grid;
- `results.csv`: one row per question and approach;
- `answers.jsonl`: the full answers, missing facts and tools used;
- `run.json`: model, settings, skipped approaches and the summary;
- `work/`: the fixture indexes, the endpoint metadata and the embeddings built for the run.

### The approaches

Every approach uses the same model (`RA_LLM_MODEL`), the same core rules (cite
`repo/path:line`, say when unsupported) and a fresh conversation per question. Skills
are left out so no approach gets instructions written for another approach's tools.

| Approach | Tools | Notes |
| --- | --- | --- |
| `json-github` | `list_repositories`, `get_repository_info`, `list_files`, `search_code`, `read_file` | The last three call the GitHub REST API (`git/trees` and `contents`), reading `--github-repo` at `--github-ref` under `--github-root/<fixture>`. Defaults: `ankmay0/repository-analyzer`, `main`, `benchmarks/fixtures`. Skipped without `GITHUB_TOKEN`. |
| `json-metadata` | `list_repositories`, `get_repository_info`, `get_endpoint_metadata` | Before the questions, the model is asked once per repository to describe every endpoint and client call (data source, source types, tables, caching, transformations, side effects, external calls, location). The cost is reported as setup tokens. |
| `tools-only` | `list_repositories`, `list_files`, `search_code`, `read_file` | No JSON index. |
| `json-embeddings` | `list_repositories`, `get_repository_info`, `semantic_search` | Files are split into 60-line chunks (15 overlap) and embedded with `BAAI/bge-small-en-v1.5` via fastembed. The top `--top-k` (8) chunks are added to each question. Skipped if fastembed is not installed. |

The JSON index is always built from the local fixture folders. For `json-github`, make
sure `--github-ref` has the same fixture code (for example, the branch or commit you are
testing).

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
end with a fake model, a mock GitHub API and a keyword embedder
(`backend/tests/test_benchmark.py`).

## From the web UI

Start the backend and the frontend as usual (see the main README) and open the
**Compare** tab. Ask a free-form question about the fixture apps (or an indexed
repository) and the four approaches answer side by side. It needs the same things
as the command line tool:

- `ANTHROPIC_API_KEY` in the backend environment, or the page shows an error and Compare
  is disabled;
- `GITHUB_TOKEN` for `json-github` (set `RA_BENCHMARK_GITHUB_REF=<branch>` until the
  fixtures are on `main`);
- `pip install -e ".[embeddings]"` for `json-embeddings`.

Approaches that are missing something appear greyed out, with the reason (the same
information is at `GET /api/compare/status`). The backend must be started from a source
checkout, or with `RA_BENCHMARKS_DIR` pointing at a directory that holds `questions.yaml`
and `fixtures/`. Graded, known-answer runs of the question set still go through the
command line tool (`python -m repo_analyzer.benchmark`).
