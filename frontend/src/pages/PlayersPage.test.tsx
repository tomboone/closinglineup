import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LEAGUE_TABLE, mockApi, renderApp } from "../test/app";

const PATH = "/api/players";
const TABLE = { [PATH]: { body: LEAGUE_TABLE } };

/** The names in the table's rows, top to bottom. */
function names() {
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent);
}

describe("PlayersPage", () => {
  it("lists the qualifying players by WS/48, the highest first", async () => {
    mockApi(TABLE);
    renderApp("/players");

    expect(await screen.findByText("4 players qualify")).toBeInTheDocument();
    expect(names()).toEqual(["Joel Embiid", "Jayson Tatum", "Tyrese Maxey", "Payton Pritchard"]);
    expect(screen.getByRole("columnheader", { name: "WS/48" })).toHaveAttribute("aria-sort", "descending");
    expect(screen.getByLabelText("Season")).toBeChecked();
  });

  it("formats each column and links the player and his team", async () => {
    mockApi(TABLE);
    renderApp("/players?date=2026-03-20");
    await screen.findByText("4 players qualify");

    const embiid = screen.getAllByRole("row")[1];
    const cells = within(embiid).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toEqual(["Joel Embiid", "PHI", "Core", "18 of 20", "33.0", "+1.0DARKO", "2.0", ".250", "0.5", "62.0%", "22.0%", "12.0"]);
    expect(within(embiid).getByRole("link", { name: "Joel Embiid" })).toHaveAttribute(
      "href",
      "/players/203954?date=2026-03-20",
    );
    expect(within(embiid).getByRole("link", { name: "PHI" })).toHaveAttribute("href", "/teams/PHI?date=2026-03-20");
  });

  it("sorts by a clicked header in ?sort=, descending, and flips it on a second click", async () => {
    mockApi(TABLE);
    const router = renderApp("/players");
    await screen.findByText("4 players qualify");

    fireEvent.click(screen.getByRole("button", { name: "TS%" }));

    expect(router.state.location.search).toBe("?sort=ts_pct");
    expect(names()).toEqual(["Payton Pritchard", "Joel Embiid", "Jayson Tatum", "Tyrese Maxey"]);

    fireEvent.click(screen.getByRole("button", { name: "TS%" }));

    expect(router.state.location.search).toBe("?sort=ts_pct&order=asc");
    expect(names()).toEqual(["Tyrese Maxey", "Jayson Tatum", "Joel Embiid", "Payton Pritchard"]);
    expect(screen.getByRole("columnheader", { name: "TS%" })).toHaveAttribute("aria-sort", "ascending");
  });

  it("sorts a missing value last either way", async () => {
    mockApi(TABLE);
    renderApp("/players?sort=impact&order=asc");
    await screen.findByText("4 players qualify");

    expect(names().at(-1)).toBe("Payton Pritchard");
  });

  it("hides a player under ?min= minutes per game, 15 by default", async () => {
    mockApi(TABLE);
    const router = renderApp("/players?min=30");
    await screen.findByText("3 players qualify");

    expect(names()).not.toContain("Payton Pritchard");
    expect(screen.getByLabelText("Min MPG")).toHaveValue(30);

    fireEvent.change(screen.getByLabelText("Min MPG"), { target: { value: "5" } });

    expect(router.state.location.search).toBe("?min=5");
    expect(names()).toContain("JD Davison");
  });

  it("hides a player with under half the window's games unless ?min_games= says otherwise", async () => {
    const rows = LEAGUE_TABLE.rows.map((row) => ({
      ...row,
      games: 10,
      games_played: row.player.name === "Tyrese Maxey" ? 2 : 10,
    }));
    mockApi({ [PATH]: { body: { ...LEAGUE_TABLE, window: "10", rows } } });
    const router = renderApp("/players?window=10");
    await screen.findByText("3 players qualify");

    expect(names()).not.toContain("Tyrese Maxey");

    fireEvent.change(screen.getByLabelText("Min games"), { target: { value: "2" } });

    expect(router.state.location.search).toBe("?window=10&min_games=2");
    expect(names()).toContain("Tyrese Maxey");
  });

  it("shows only the ?team= teams' players", async () => {
    mockApi(TABLE);
    renderApp("/players?team=PHI");
    await screen.findByText("2 players qualify");

    expect(names()).toEqual(["Joel Embiid", "Tyrese Maxey"]);
  });

  it("fetches the window picked, and ?window=prior_season with it", async () => {
    const fetchMock = mockApi(TABLE);
    const router = renderApp("/players?window=prior_season");
    await screen.findByText("4 players qualify");

    expect(fetchMock).toHaveBeenCalledWith("/api/players?window=prior_season");
    expect(screen.getByLabelText("Prior season")).toBeChecked();

    fireEvent.click(screen.getByLabelText("Season"));

    expect(router.state.location.search).toBe("");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/players"));
  });

  it("says which minimums to lower when no player qualifies", async () => {
    mockApi(TABLE);
    renderApp("/players?min=40");

    expect(
      await screen.findByText("No player qualifies: lower the minimum minutes per game."),
    ).toBeInTheDocument();
  });
});
