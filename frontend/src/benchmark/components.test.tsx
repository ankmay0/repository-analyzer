import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { BarChart } from "./BarChart";
import { CellDetail } from "./CellDetail";
import { APPROACHES, QUESTIONS, row, STATUS } from "./fixtures";
import { ResultsGrid } from "./ResultsGrid";
import { RunForm } from "./RunForm";
import { summarize } from "./model";
import { SummaryCards } from "./SummaryCards";

const noop = () => undefined;

function form(overrides: Partial<Parameters<typeof RunForm>[0]> = {}) {
  return renderToStaticMarkup(
    <RunForm
      status={STATUS}
      questions={QUESTIONS}
      approaches={new Set(["json-metadata", "tools-only"])}
      selectedQuestions={new Set(QUESTIONS.map((q) => q.id))}
      judge={false}
      githubRef="main"
      running={false}
      onToggleApproach={noop}
      onToggleQuestion={noop}
      onSelectQuestions={noop}
      onJudge={noop}
      onGithubRef={noop}
      onRun={noop}
      {...overrides}
    />,
  );
}

describe("RunForm", () => {
  it("disables unavailable approaches and shows why", () => {
    const html = form();
    const github = html.match(/<li[^>]*data-approach="json-github".*?<\/li>/s)?.[0] ?? "";

    expect(github).toContain("disabled");
    expect(github).toContain("Unavailable: GITHUB_TOKEN is not set on the server");
    const metadata = html.match(/<li[^>]*data-approach="json-metadata".*?<\/li>/s)?.[0] ?? "";
    expect(metadata).not.toContain("disabled");
    expect(metadata).toContain("checked");
  });

  it("labels the run button with what will run, and offers the question picker", () => {
    const html = form();
    expect(html).toContain("Run 2 approaches × 2 questions");
    expect(html).toContain("Questions: 2 of 2 selected");
    expect(html).toContain("summary-data-source");
  });

  it("disables Run without a key, with nothing chosen, and while running", () => {
    const runButton = (html: string) => html.match(/<button type="submit"[^>]*>/)?.[0] ?? "";
    expect(runButton(form())).not.toContain("disabled");
    expect(runButton(form({ approaches: new Set() }))).toContain("disabled");
    expect(runButton(form({ selectedQuestions: new Set() }))).toContain("disabled");
    expect(runButton(form({ running: true }))).toContain("disabled");

    const noKey = form({ status: { ...STATUS, llm_configured: false, llm_message: "ANTHROPIC_API_KEY is not set on the server" } });
    expect(runButton(noKey)).toContain("disabled");
    expect(noKey).toContain("ANTHROPIC_API_KEY is not set on the server");
  });

  it("only asks for a GitHub ref when json-github is chosen", () => {
    expect(form()).not.toContain("GitHub ref");
    const available = { ...STATUS, approaches: APPROACHES.map((a) => ({ ...a, available: true, reason: null })) };
    expect(form({ status: available, approaches: new Set(["json-github"]) })).toContain("GitHub ref");
  });
});

describe("ResultsGrid", () => {
  const rows = [
    row({ approach: "tools-only" }),
    row({ approach: "tools-only", question_id: "tax-rate", correct: false, facts_matched: 1, facts_total: 3, total_tokens: 900, latency_ms: 1200 }),
    row({ approach: "json-metadata", correct: false, facts_matched: 0, facts_total: 2, error: "x" }),
  ];

  function grid(selected: { approach: string; questionId: string } | null = null) {
    return renderToStaticMarkup(
      <ResultsGrid
        questions={QUESTIONS}
        approaches={["tools-only", "json-metadata", "json-embeddings", "json-github"]}
        rows={rows}
        skipped={{ "json-embeddings": "no embedder" }}
        current={{ approach: "json-metadata", question_id: "tax-rate", phase: "asking" }}
        selected={selected}
        onSelect={noop}
      />,
    );
  }

  it("shows a badge, facts, tokens and latency in each answered cell", () => {
    const html = grid();
    expect(html).toContain('class="badge badge-pass">Pass');
    expect(html).toContain('class="badge badge-partial">Partial');
    expect(html).toContain('class="badge badge-fail">Fail');
    expect(html).toContain("2/2 facts");
    expect(html).toContain("1/3 facts");
    expect(html).toContain("2.5k tok · 8.4 s");
    expect(html).toContain("900 tok · 1.2 s");
  });

  it("marks unanswered cells as running, skipped or pending", () => {
    const html = grid();
    expect(html).toContain("running…"); // json-metadata / tax-rate
    expect(html).toContain('title="no embedder">skipped');
    expect(html).toContain(">·<");
  });

  it("highlights the selected cell", () => {
    const html = grid({ approach: "tools-only", questionId: "tax-rate" });
    expect(html.match(/cell-selected/g)).toHaveLength(1);
    expect(html).toContain('aria-pressed="true"');
  });
});

describe("SummaryCards and BarChart", () => {
  const summaries = summarize(
    [row({ approach: "tools-only", total_tokens: 2000, latency_ms: 4000 }), row({ approach: "json-metadata", total_tokens: 500, latency_ms: 1000, correct: false, facts_matched: 1, facts_total: 2 })],
    ["tools-only", "json-metadata"],
  );

  it("shows correctness, average tokens and latency for each approach", () => {
    const html = renderToStaticMarkup(<SummaryCards summaries={summaries} />);
    expect(html).toContain('<p class="summary-big">100%</p>');
    expect(html).toContain('<p class="summary-big">0%</p>');
    expect(html).toContain("2.0k");
    expect(html).toContain("4.0 s");
    expect(html.match(/<svg/g)).toHaveLength(3);
  });

  it("scales bar lengths to the largest value", () => {
    const html = renderToStaticMarkup(
      <BarChart title="Tokens" bars={[{ label: "a", value: 100, text: "100" }, { label: "b", value: 50, text: "50" }]} />,
    );
    const widths = [...html.matchAll(/class="bar-fill"/g)].length;
    expect(widths).toBe(2);
    expect(html).toContain('width="200" height="16" rx="3" class="bar-fill"');
    expect(html).toContain('width="100" height="16" rx="3" class="bar-fill"');
  });
});

describe("CellDetail", () => {
  it("lists matched and missed key facts and shows the full answer", () => {
    const html = renderToStaticMarkup(
      <CellDetail
        row={row({ correct: false, facts_matched: 1, matched_facts: ["orders"], missing_facts: ["60 | 1 minute"], answer: "It reads the **orders** table.", judge_correct: false, judge_reason: "no TTL" })}
        question={QUESTIONS[0]!}
        onClose={noop}
      />,
    );
    expect(html).toContain('class="fact-matched"');
    expect(html).toContain('class="fact-missed"');
    expect(html).toContain("Key facts (1 of 2 found)");
    expect(html).toContain("<strong>orders</strong>");
    expect(html).toContain("Reference answer");
    expect(html).toContain("2,300 in / 170 out");
    expect(html).toContain("LLM judge: <strong>incorrect</strong> — no TTL");
  });
});
