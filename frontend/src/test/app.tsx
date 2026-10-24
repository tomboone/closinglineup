import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { vi } from "vitest";
import type { MetaResponse, TeamsResponse } from "../api/client";
import { routes } from "../routes";

/** A mocked API reply: a JSON body with a status (200 if omitted), or a request that never ends. */
export type Reply = { status?: number; body: unknown } | "pending";

export const FRESH_META: MetaResponse = {
  latest_derived_date: "2026-03-20",
  jobs: [],
  freshness: [
    { source: "box_score", latest: "2026-03-20" },
    { source: "injury_pdf", latest: "2026-03-20T18:30:00-04:00" },
    { source: "injury_espn", latest: null },
    { source: "transactions", latest: "2026-03-20" },
  ],
  injury_sources: [{ tricode: "BOS", source: "pdf", stale: false }],
};

export const TEAMS: TeamsResponse = {
  season: "2025-26",
  teams: [
    {
      tricode: "BOS",
      name: "Boston Celtics",
      conference: "East",
      division: "Atlantic",
      wins: 50,
      losses: 20,
      latest_derived_date: "2026-03-20",
    },
    {
      tricode: "LAL",
      name: "Los Angeles Lakers",
      conference: "West",
      division: "Pacific",
      wins: 40,
      losses: 30,
      latest_derived_date: "2026-03-19",
    },
  ],
};

/**
 * Stubs `fetch` with replies by path, `/api/meta` and `/api/teams` answering fresh by default; a
 * path with no reply is a 404. Returns the mock, whose calls are the URLs fetched.
 */
export function mockApi(replies: Record<string, Reply> = {}) {
  const all: Record<string, Reply> = {
    "/api/meta": { body: FRESH_META },
    "/api/teams": { body: TEAMS },
    ...replies,
  };
  const fetchMock = vi.fn((input: string) => {
    const reply = all[new URL(input, "http://localhost").pathname];
    if (reply === "pending") return new Promise<Response>(() => {});
    const { status = 200, body } = reply ?? { status: 404, body: { detail: "Not Found" } };
    return Promise.resolve(Response.json(body, { status }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Renders the app at `path` with a query client of its own; returns the router. */
export function renderApp(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}
