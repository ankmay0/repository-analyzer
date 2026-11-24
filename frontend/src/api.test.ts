import { afterEach, describe, expect, it, vi } from "vitest";

import { getHealth, parseHealth } from "./api";

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
