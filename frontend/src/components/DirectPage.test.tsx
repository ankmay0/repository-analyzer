import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { DirectAskResponse, Workspace } from "../api";
import { DirectPage, DirectResult } from "./DirectPage";

const workspace: Workspace = {
  repositories: [
    {
      name: "core-lib",
      owner: "local",
      source: "/tmp/core-lib",
      commit: null,
      default_branch: null,
      file_count: 2,
      symbol_count: 1,
      dependency_count: 0,
      index_file: "local__core-lib.json",
    },
    {
      name: "web-app",
      owner: "local",
      source: "/tmp/web-app",
      commit: null,
      default_branch: null,
      file_count: 3,
      symbol_count: 1,
      dependency_count: 1,
      index_file: "local__web-app.json",
    },
  ],
  indexed_at: null,
  job: { state: "idle", message: "", error: null, started_at: null, finished_at: null },
};

const result: DirectAskResponse = {
  repo: "core-lib",
  question: "Where is sign_token?",
  answer: "It is defined in `src/core_lib/tokens.py`.",
  tool_calls: [
    { name: "search_code", arguments: { pattern: "sign_token" }, result: "tokens.py:1" },
    { name: "read_file", arguments: { path: "src/core_lib/tokens.py" }, result: "def sign_token" },
  ],
  usage: {
    input_tokens: 100,
    output_tokens: 20,
    total_tokens: 120,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
    llm_calls: 2,
    tool_calls: 2,
    latency_ms: 1500,
  },
};

describe("DirectPage", () => {
  it("explains the mode and offers indexed repository names", () => {
    const html = renderToStaticMarkup(<DirectPage workspace={workspace} />);
    expect(html).toContain("Direct analysis");
    expect(html).toContain("No Ctags JSON index");
    expect(html).toContain('id="direct-repo"');
    expect(html).toContain('id="direct-question"');
    expect(html).toContain("core-lib");
    expect(html).toContain("web-app");
    expect(html).toContain("Ask");
  });
});

describe("DirectResult", () => {
  it("shows a loading line while waiting", () => {
    const html = renderToStaticMarkup(<DirectResult pending={true} error={null} result={null} />);
    expect(html).toContain("Working…");
  });

  it("renders the markdown answer, tool steps and usage", () => {
    const html = renderToStaticMarkup(<DirectResult pending={false} error={null} result={result} />);
    expect(html).toContain("Where is sign_token?");
    expect(html).toContain("core-lib");
    expect(html).toContain("2 research steps");
    expect(html).toContain("search_code (pattern: sign_token)");
    expect(html).toContain("<code>src/core_lib/tokens.py</code>");
    expect(html).toContain("120 tokens");
    expect(html).toContain("2 tool calls");
  });

  it("shows an error message", () => {
    const html = renderToStaticMarkup(
      <DirectResult pending={false} error="ANTHROPIC_API_KEY is not set on the server" result={null} />,
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain("ANTHROPIC_API_KEY is not set on the server");
  });
});
