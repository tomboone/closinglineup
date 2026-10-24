import { describe, expect, it, vi } from "vitest";
import { ApiError, apiUrl, getTeam } from "./client";

describe("apiUrl", () => {
  it("leaves out unset parameters", () => {
    expect(apiUrl("/api/league", { date: undefined })).toBe("/api/league");
    expect(apiUrl("/api/teams/BOS", { date: null, garbage_time: "include" })).toBe(
      "/api/teams/BOS?garbage_time=include",
    );
  });
});

describe("fetchers", () => {
  it("raise the API's detail with the status", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(Response.json({ detail: "No team 'XXX'" }, { status: 404 })),
    );

    const error = await getTeam("XXX").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, message: "No team 'XXX'" });
  });

  it("fall back to the status line when the body has no string detail", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(new Response("oops", { status: 502, statusText: "Bad Gateway" })),
    );

    await expect(getTeam("BOS")).rejects.toMatchObject({ status: 502, message: "502 Bad Gateway" });
  });
});
