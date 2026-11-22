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
