import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EMPTY_LEAGUE_PAGE, LEAGUE_PAGE, mockApi, renderApp } from "../test/app";
import { allOptions } from "../test/echarts";

vi.mock("../components/charts/echarts", () => import("../test/echarts"));

const SECTIONS = [
  "Rotation changes",
  "Minutes movers",
  "Impact movers",
  "Flags",
];

/** `n` movers one way, each a player of his own. */
const movers = (n: number, sign: 1 | -1, source: "epm" | null = null) =>
  Array.from({ length: n }, (_, i) => ({
    player: { player_id: 1000 + i, name: `Player ${i}` },
    tricode: "BOS",
    value: 0.1,
    delta: sign * (0.05 - i / 1000),
    source,
  }));

describe("LeaguePage", () => {
  it("renders every rotation event under its team, from → to, with its three signals", async () => {
    mockApi({ "/api/league": { body: LEAGUE_PAGE } });
    renderApp("/");
    const events = await screen.findByRole("region", { name: "Rotation changes" });

    for (const team of LEAGUE_PAGE.rotation_events) {
      const section = within(events).getByRole("region", { name: team.tricode });
      expect(within(section).getByRole("link", { name: team.tricode })).toHaveAttribute(
        "href",
        `/teams/${team.tricode}`,
      );
      const items = within(section).getAllByRole("listitem").filter((li) => li.querySelector("ul"));
      expect(items).toHaveLength(team.events.length);
      team.events.forEach((event, i) => {
        const item = items[i]!;
        expect(within(item).getByRole("link", { name: event.player.name! })).toBeInTheDocument();
        const signals = within(within(item).getByRole("list")).getAllByRole("listitem");
        expect(signals.map((li) => li.textContent)).toEqual(event.signals.map((s) => s.label));
      });
    }
    const bos = within(events).getByRole("region", { name: "BOS" });
    expect(bos).toHaveTextContent("Payton Pritchard Fringe → Core");
    expect(within(events).getByRole("region", { name: "LAL" })).toHaveTextContent(
      "Dalton Knecht New → Fringe",
    );
  });

  it("renders a day no team played as 'no games' in every section", async () => {
    mockApi({ "/api/league": { body: EMPTY_LEAGUE_PAGE } });
    renderApp("/?date=2026-03-21");
    await screen.findByText("Morning feed for 2026-03-21.");

    for (const name of SECTIONS) {
      expect(screen.getByRole("region", { name })).toHaveTextContent("No games on 2026-03-21.");
    }
    expect(screen.getByRole("region", { name: "Injury changes" })).toHaveTextContent(
      "No injury changes.",
    );
    expect(screen.getByRole("region", { name: "Transactions" })).toHaveTextContent(
      "No transactions.",
    );
    expect(allOptions()).toHaveLength(0);
  });

  it("still says 'no games' on a day no team played that has a transaction", async () => {
    const page = { ...EMPTY_LEAGUE_PAGE, transactions: LEAGUE_PAGE.transactions };
    mockApi({ "/api/league": { body: page } });
    renderApp("/?date=2026-03-21");

    const events = await screen.findByRole("region", { name: "Rotation changes" });
    expect(events).toHaveTextContent("No games on 2026-03-21.");
    expect(screen.getByRole("region", { name: "Transactions" })).toHaveTextContent("Signed");
  });

  it("says an empty section is empty, not 'no games', on a day with games", async () => {
    mockApi({ "/api/league": { body: { ...LEAGUE_PAGE, transactions: [] } } });
    renderApp("/");

    const transactions = await screen.findByRole("region", { name: "Transactions" });
    expect(transactions).toHaveTextContent("No transactions.");
  });

  it("caps the movers at the API's counts, each linking to its player page", async () => {
    const page = {
      ...LEAGUE_PAGE,
      minutes_risers: movers(20, 1),
      minutes_fallers: movers(20, -1),
      impact_risers: movers(12, 1, "epm"),
      impact_fallers: movers(12, -1, "epm"),
    };
    mockApi({ "/api/league": { body: page } });
    renderApp("/?date=2026-03-20");
    const minutes = await screen.findByRole("region", { name: "Minutes movers" });
    const impact = screen.getByRole("region", { name: "Impact movers" });

    const rows = (region: HTMLElement, caption: string) =>
      within(within(region).getByRole("table", { name: caption })).getAllByRole("row").slice(1);
    for (const caption of ["Rising", "Falling"]) {
      expect(rows(minutes, caption)).toHaveLength(15);
      expect(rows(impact, caption)).toHaveLength(10);
    }
    expect(within(rows(minutes, "Rising")[0]!).getByRole("link", { name: "Player 0" })).toHaveAttribute(
      "href",
      "/players/1000?date=2026-03-20",
    );
    expect(within(rows(minutes, "Rising")[0]!).getByText("+5.0 pts")).toBeInTheDocument();
    expect(within(rows(impact, "Falling")[0]!).getByText("EPM")).toBeInTheDocument();
    // A sparkline a mover.
    expect(allOptions()).toHaveLength(2 * 15 + 2 * 10);
  });

  it("groups flags by flag and lists injury changes and transactions with their links", async () => {
    mockApi({ "/api/league": { body: LEAGUE_PAGE } });
    renderApp("/");

    const flags = await screen.findByRole("region", { name: "Flags" });
    const cliff = within(flags).getByRole("region", { name: "Minutes cliff" });
    expect(cliff).toHaveTextContent("Jrue Holiday BOS 5-game share 5.0% vs 20-game 12.0%");
    expect(within(flags).getByRole("region", { name: "Contract pressure" })).toHaveTextContent(
      "Two-way: 46 of 50 active games",
    );

    const injuries = screen.getByRole("region", { name: "Injury changes" });
    expect(injuries).toHaveTextContent("Kristaps Porzingis BOS Not listed → Out");
    expect(within(injuries).getByRole("link", { name: "BOS" })).toHaveAttribute("href", "/teams/BOS");

    const transactions = screen.getByRole("region", { name: "Transactions" });
    expect(within(transactions).getByRole("link", { name: "Dalton Knecht" })).toHaveAttribute(
      "href",
      "/players/1642000",
    );
    expect(within(transactions).getByRole("link", { name: "LAL" })).toBeInTheDocument();
  });
});
