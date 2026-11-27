import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, askDirect, getHealth, parseDirectAsk, parseHealth } from "./api";

describe("parseHealth", () => {
  it("reads the health payload and treats missing flags as off", () => {
    expect(
      parseHealth({
        status: "ok",
        llm_configured: true,
        llm_model: "claude-opus-5-5",
        ctags_available: true,
        ctags_version: "Universal Ctags 6.0.0",
        graph_available: true,
      }),
    ).toEqual({
      status: "ok",
      llm_configured: true,
      llm_model: "claude-opus-5-5",
      ctags_available: true,
      ctags_version: "Universal Ctags 6.0.0",
      graph_available: true,
    });

    expect(parseHealth({})).toEqual({
      status: "ok",
      llm_configured: false,
      llm_model: "",
      ctags_available: false,
      ctags_version: null,
      graph_available: false,
    });
  });
});

describe("getHealth", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("GETs /api/health and parses the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "ok",
          llm_configured: true,
          llm_model: "claude-opus-5-5",
          ctags_available: true,
          ctags_version: "Universal Ctags 6.0.0",
          graph_available: false,
        }),
        { headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getHealth()).resolves.toMatchObject({
      llm_configured: true,
      ctags_available: true,
      graph_available: false,
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/health");
  });

  it("raises an ApiError when the request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "backend down" }), { status: 500 })),
    );
    await expect(getHealth()).rejects.toMatchObject({ status: 500, message: "backend down" });
  });
});


describe("parseDirectAsk", () => {
  it("reads the answer and tool calls, and treats missing usage as null", () => {
    expect(
      parseDirectAsk({
        repo: "core-lib",
        question: "Where is sign_token?",
        answer: "In tokens.py.",
        tool_calls: [
          {
            name: "search_code",
            arguments: { pattern: "sign_token" },
            result: "src/core_lib/tokens.py:1: def sign_token",
          },
        ],
      }),
    ).toEqual({
      repo: "core-lib",
      question: "Where is sign_token?",
      answer: "In tokens.py.",
      tool_calls: [
        {
          name: "search_code",
          arguments: { pattern: "sign_token" },
          result: "src/core_lib/tokens.py:1: def sign_token",
        },
      ],
      usage: null,
    });
  });

  it("parses usage when the server includes it", () => {
    const parsed = parseDirectAsk({
      repo: "core-lib",
      question: "q",
      answer: "a",
      tool_calls: [],
      usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15, tool_calls: 1, llm_calls: 2, latency_ms: 900 },
    });
    expect(parsed.usage).toMatchObject({ input_tokens: 10, output_tokens: 5, total_tokens: 15, tool_calls: 1 });
  });

  it("ignores a malformed body", () => {
    expect(parseDirectAsk(null)).toEqual({
      repo: "",
      question: "",
      answer: "",
      tool_calls: [],
      usage: null,
    });
  });
});

describe("askDirect", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("POSTs /api/direct/ask and parses the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          repo: "core-lib",
          question: "What is this?",
          answer: "A small library.",
          tool_calls: [],
        }),
        { headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(askDirect("core-lib", "What is this?")).resolves.toMatchObject({
      repo: "core-lib",
      answer: "A small library.",
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/direct/ask");
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1].body as string)).toEqual({
      repo: "core-lib",
      question: "What is this?",
    });
  });

  it("raises an ApiError when the request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "ANTHROPIC_API_KEY is not set on the server" }), { status: 503 }),
      ),
    );
    await expect(askDirect("core-lib", "What is this?")).rejects.toMatchObject({
      status: 503,
      message: "ANTHROPIC_API_KEY is not set on the server",
    });
    await expect(askDirect("core-lib", "What is this?")).rejects.toBeInstanceOf(ApiError);
  });
});
