import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { easternToday } from "../shell/params";
import { EMPTY_LEAGUE_PAGE, LEAGUE_PAGE, mockApi, renderApp, SLATE_PAGE } from "../test/app";
import { allOptions } from "../test/echarts";

vi.mock("../components/charts/echarts", () => import("../test/echarts"));

// The morning after `LEAGUE_PAGE`'s date, whose last night is that date.
const MORNING = "/?date=2026-03-21";

/** The API for a morning: its slate with the given last night, and last night's league page. */
const morning = (league: unknown = LEAGUE_PAGE, slate: unknown = SLATE_PAGE) =>
  mockApi({ "/api/slate": { body: slate }, "/api/league": { body: league } });

/** `n` movers one way, each a player of his own. */
const movers = (n: number, sign: 1 | -1, source: "epm" | null = null) =>
  Array.from({ length: n }, (_, i) => ({
    player: { player_id: 1000 + i, name: `Player ${i}` },
    tricode: "BOS",
    value: 0.1,
    delta: sign * (0.05 - i / 1000),
    source,
    impact: null,
  }));

const openTab = (name: string) => fireEvent.click(screen.getByRole("tab", { name }));

describe("LeaguePage", () => {
  it("opens on Today, the morning's slate, with both dates in the header", async () => {
    const fetchMock = morning();
    renderApp(MORNING);

    expect(await screen.findByText("Last night: 2026-03-20")).toBeInTheDocument();
    expect(screen.getByText("Today: 2026-03-21")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Today" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "Today" })).toHaveTextContent(
      "1 game on 2026-03-21.",
    );
    expect(fetchMock).toHaveBeenCalledWith("/api/slate?date=2026-03-21");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/league?date=2026-03-20"));
  });

  it("says when last night lags the day before the morning", async () => {
    morning(LEAGUE_PAGE, { ...SLATE_PAGE, date: "2026-03-22" });
    renderApp("/?date=2026-03-22");

    expect(
      await screen.findByText("Last night: 2026-03-20 (2026-03-21 not derived yet)"),
    ).toBeInTheDocument();
  });

  it("says there is no last night before the first derived date, and reads no league page", async () => {
    const fetchMock = morning(LEAGUE_PAGE, { ...SLATE_PAGE, last_night: null });
    renderApp(`${MORNING}&tab=rotation`);

    expect(await screen.findByText("Last night: none yet")).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "Rotation" })).toHaveTextContent(
      "No last night yet: no game date before 2026-03-21 has been derived.",
    );
    expect(fetchMock.mock.calls.map(([url]) => url)).not.toContainEqual(
      expect.stringContaining("/api/league"),
    );
  });

  it("says a morning with no games has none", async () => {
    morning(LEAGUE_PAGE, { ...SLATE_PAGE, games: [] });
    renderApp(MORNING);

    expect(await screen.findByRole("tabpanel", { name: "Today" })).toHaveTextContent(
      "No games on 2026-03-21.",
    );
  });

  it("defaults the morning to today in Eastern time", async () => {
    const fetchMock = morning();
    renderApp("/");

    await screen.findByText(`Today: ${easternToday()}`);
    expect(fetchMock).toHaveBeenCalledWith(`/api/slate?date=${easternToday()}`);
  });

  it("switches tabs in ?tab= without refetching", async () => {
    const fetchMock = morning();
    const router = renderApp(MORNING);
    await screen.findByText("Last night: 2026-03-20");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/league?date=2026-03-20"));
    const fetches = fetchMock.mock.calls.length;

    openTab("Flags");
    expect(router.state.location.search).toBe("?date=2026-03-21&tab=flags");
    expect(await screen.findByRole("region", { name: "Flags" })).toBeInTheDocument();
    openTab("Movers");
    expect(screen.getByRole("region", { name: "Minutes movers" })).toBeInTheDocument();
    openTab("Today");
    expect(router.state.location.search).toBe("?date=2026-03-21");

    expect(fetchMock).toHaveBeenCalledTimes(fetches);
  });

  it("renders every rotation event under its team, from → to, with its three signals", async () => {
    morning();
    renderApp(`${MORNING}&tab=rotation`);
    const events = await screen.findByRole("region", { name: "Rotation changes" });

    for (const team of LEAGUE_PAGE.rotation_events) {
      const section = within(events).getByRole("region", { name: team.tricode });
      expect(within(section).getByRole("link", { name: team.tricode })).toHaveAttribute(
        "href",
        `/teams/${team.tricode}?date=2026-03-21&tab=rotation`,
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

  it("says 'no games' on every derived tab when last night had none", async () => {
    morning(EMPTY_LEAGUE_PAGE, { ...SLATE_PAGE, date: "2026-03-22", last_night: "2026-03-21" });
    renderApp("/?date=2026-03-22&tab=rotation");

    expect(await screen.findByRole("region", { name: "Rotation changes" })).toHaveTextContent(
      "No games on 2026-03-21.",
    );
    openTab("Movers");
    for (const name of ["Minutes movers", "Impact movers"]) {
      expect(screen.getByRole("region", { name })).toHaveTextContent("No games on 2026-03-21.");
    }
    openTab("Flags");
    expect(screen.getByRole("region", { name: "Flags" })).toHaveTextContent(
      "No games on 2026-03-21.",
    );
    openTab("Moves");
    expect(screen.getByRole("region", { name: "Transactions" })).toHaveTextContent(
      "No transactions.",
    );
    expect(allOptions()).toHaveLength(0);
  });

  it("says an empty section is empty, not 'no games', on a night with games", async () => {
    morning({ ...LEAGUE_PAGE, flags: [] });
    renderApp(`${MORNING}&tab=flags`);

    expect(await screen.findByRole("region", { name: "Flags" })).toHaveTextContent(
      "No flags raised.",
    );
  });

  it("caps the movers at the API's counts, each linking to its player page", async () => {
    morning({
      ...LEAGUE_PAGE,
      minutes_risers: movers(20, 1),
      minutes_fallers: movers(20, -1),
      impact_risers: movers(12, 1, "epm"),
      impact_fallers: movers(12, -1, "epm"),
    });
    renderApp(`${MORNING}&tab=movers`);
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
      "/players/1000?date=2026-03-21&tab=movers",
    );
    expect(within(rows(minutes, "Rising")[0]!).getByText("+5.0 pts")).toBeInTheDocument();
    expect(within(rows(impact, "Falling")[0]!).getByText("EPM")).toBeInTheDocument();
    // A sparkline a mover.
    expect(allOptions()).toHaveLength(2 * 15 + 2 * 10);
  });

  it("groups flags by flag", async () => {
    morning();
    renderApp(`${MORNING}&tab=flags`);

    const flags = await screen.findByRole("region", { name: "Flags" });
    const cliff = within(flags).getByRole("region", { name: "Minutes cliff" });
    expect(cliff).toHaveTextContent("Jrue Holiday BOS 5-game share 5.0% vs 20-game 12.0%");
    expect(within(flags).getByRole("region", { name: "Contract pressure" })).toHaveTextContent(
      "Two-way: 46 of 50 active games",
    );
  });

  it("lists last night's and the morning's transactions, each dated", async () => {
    const tomorrows = { ...LEAGUE_PAGE.transactions[0]!, date: "2026-03-21", type: "waive" };
    morning({ ...LEAGUE_PAGE, transactions: [...LEAGUE_PAGE.transactions, tomorrows] });
    renderApp(`${MORNING}&tab=moves`);

    const transactions = await screen.findByRole("region", { name: "Transactions" });
    const items = within(transactions).getAllByRole("listitem");
    expect(items.map((li) => li.textContent?.slice(0, 10))).toEqual(["2026-03-20", "2026-03-21"]);
    expect(within(items[0]!).getByRole("link", { name: "Dalton Knecht" })).toHaveAttribute(
      "href",
      "/players/1642000?date=2026-03-21&tab=moves",
    );
    expect(within(items[0]!).getByRole("link", { name: "LAL" })).toBeInTheDocument();
  });

  it("reads the morning's transactions off the day before's page when last night lags", async () => {
    const transaction = (date: string, name: string) => ({
      ...LEAGUE_PAGE.transactions[0]!,
      date,
      player: { player_id: 1, name },
    });
    const fetchMock = mockApi({
      "/api/slate": { body: { ...SLATE_PAGE, date: "2026-03-22" } },
      // The route answers both dates; each page's transactions are its date's and the day after's.
      "/api/league": {
        body: {
          ...LEAGUE_PAGE,
          transactions: [
            transaction("2026-03-20", "Last night's"),
            transaction("2026-03-21", "Underived night's"),
            transaction("2026-03-22", "Morning's"),
          ],
        },
      },
    });
    renderApp("/?date=2026-03-22&tab=moves");

    const transactions = await screen.findByRole("region", { name: "Transactions" });
    expect(fetchMock).toHaveBeenCalledWith("/api/league?date=2026-03-20");
    expect(fetchMock).toHaveBeenCalledWith("/api/league?date=2026-03-21");
    const players = within(transactions).getAllByRole("link", { name: /night's|Morning's/ });
    expect(players.map((link) => link.textContent)).toEqual(["Last night's", "Morning's"]);
  });

  describe("team filter", () => {
    const LAL_FLAG = {
      ...LEAGUE_PAGE.flags[0]!,
      player: { player_id: 1642000, name: "Dalton Knecht" },
      tricode: "LAL",
    };
    const TRADE = {
      date: "2026-03-20",
      type: "trade",
      detail: "Boston traded Jrue Holiday to Los Angeles.",
      player: { player_id: 201950, name: "Jrue Holiday" },
      from_tricode: "BOS",
      to_tricode: "LAL",
      impact: null,
    };
    const PAGE = { ...LEAGUE_PAGE, flags: [...LEAGUE_PAGE.flags, LAL_FLAG] };

    it("shows only the selected teams' rows on every derived tab", async () => {
      morning({ ...PAGE, transactions: [...PAGE.transactions, TRADE] });
      renderApp(`${MORNING}&tab=flags&team=LAL`);

      const flags = await screen.findByRole("region", { name: "Flags" });
      expect(within(flags).getAllByRole("listitem")).toHaveLength(1);
      expect(flags).toHaveTextContent("Dalton Knecht LAL");

      openTab("Rotation");
      const events = screen.getByRole("region", { name: "Rotation changes" });
      expect(within(events).queryByRole("region", { name: "BOS" })).not.toBeInTheDocument();
      expect(within(events).getByRole("region", { name: "LAL" })).toBeInTheDocument();

      openTab("Movers");
      expect(screen.getByRole("region", { name: "Minutes movers" })).toHaveTextContent(
        "No minutes movers.",
      );

      // A transaction by either of its teams.
      openTab("Moves");
      const moves = within(screen.getByRole("region", { name: "Transactions" }));
      expect(moves.getAllByRole("listitem")).toHaveLength(2);
    });

    it("puts the checked teams in ?team=, keeping the other parameters", async () => {
      morning(PAGE);
      const router = renderApp(`${MORNING}&tab=flags`);
      const flags = await screen.findByRole("region", { name: "Flags" });
      expect(within(flags).getAllByRole("listitem")).toHaveLength(3);

      fireEvent.click(await screen.findByRole("checkbox", { name: "LAL" }));
      expect(router.state.location.search).toBe("?date=2026-03-21&tab=flags&team=LAL");
      expect(screen.getByText("Teams: LAL")).toBeInTheDocument();
      expect(within(flags).getAllByRole("listitem")).toHaveLength(1);

      fireEvent.click(screen.getByRole("checkbox", { name: "BOS" }));
      expect(router.state.location.search).toBe("?date=2026-03-21&tab=flags&team=LAL%2CBOS");
      expect(within(flags).getAllByRole("listitem")).toHaveLength(3);

      fireEvent.click(screen.getByRole("button", { name: "All teams" }));
      expect(router.state.location.search).toBe("?date=2026-03-21&tab=flags");
      expect(screen.getByText("Teams: all")).toBeInTheDocument();
    });
  });
});
