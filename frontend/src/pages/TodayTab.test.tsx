import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SlatePage } from "../api/client";
import { LEAGUE_PAGE, mockApi, renderApp, SLATE_PAGE } from "../test/app";

vi.mock("../components/charts/echarts", () => import("../test/echarts"));

type SlateTeam = SlatePage["games"][number]["home"];
type SlateLine = SlateTeam["lines"][number];

const MORNING = "/?date=2026-03-21";

const player = (player_id: number, name: string) => ({ player_id, name });

const line = (ref: { player_id: number; name: string }, fields: Partial<SlateLine>): SlateLine => ({
  player: ref,
  status: null,
  reason: null,
  previous_status: null,
  change: null,
  g_league: false,
  tier: "core",
  minutes_share_10: 0.5,
  impact: { source: "darko", value: 1.25 },
  ...fields,
});

const starter = (ref: { player_id: number; name: string }, replacement = false) => ({
  player: ref,
  starter_rate_5: 1,
  minutes_share_10: 0.7,
  impact: null,
  replacement,
});

const TATUM = player(1628369, "Jayson Tatum");
const BROWN = player(1627759, "Jaylen Brown");
const WHITE = player(1628401, "Derrick White");
const HOLIDAY = player(201950, "Jrue Holiday");
const PORZINGIS = player(204001, "Kristaps Porzingis");
const PRITCHARD = player(1630202, "Payton Pritchard");

/** Boston's report with a line of each change kind and an unchanged one, and a replacement starter. */
const BOS: SlateTeam = {
  tricode: "BOS",
  b2b: false,
  report: {
    report_datetime: "2026-03-21T11:30:00-04:00",
    source: "pdf",
    stale: false,
    submitted: true,
  },
  lines: [
    line(TATUM, { status: "Out", reason: "Achilles", change: "new", tier: "injured" }),
    line(PORZINGIS, {
      status: "Questionable",
      reason: "Illness",
      previous_status: "Probable",
      change: "downgraded",
    }),
    line(BROWN, {
      status: "Available",
      previous_status: "Doubtful",
      change: "upgraded",
      tier: "starter",
    }),
    line(PRITCHARD, { status: "Probable", reason: "Ankle", previous_status: "Probable" }),
    line(HOLIDAY, { previous_status: "Questionable", change: "off" }),
  ],
  projected_starters: [
    starter(BROWN),
    starter(WHITE),
    starter(HOLIDAY),
    starter(PORZINGIS),
    starter(PRITCHARD, true),
  ],
};

/** The Lakers on their second night, with no report yet and a player on G League assignment. */
const LAL: SlateTeam = {
  tricode: "LAL",
  b2b: true,
  report: { report_datetime: null, source: null, stale: false, submitted: false },
  lines: [
    line(player(1642000, "Dalton Knecht"), {
      g_league: true,
      tier: null,
      minutes_share_10: null,
      impact: null,
    }),
  ],
  projected_starters: [],
};

const team = (tricode: string): SlateTeam => ({ ...LAL, tricode, b2b: false, lines: [] });

/** Philadelphia's report is ESPN's fallback, listing nobody. */
const PHI: SlateTeam = {
  ...team("PHI"),
  report: {
    report_datetime: "2026-03-21T09:00:00-04:00",
    source: "espn",
    stale: true,
    submitted: true,
  },
};

const SLATE: SlatePage = {
  ...SLATE_PAGE,
  games: [
    { game_id: "0022501000", tip_time: "2026-03-21T19:30:00-04:00", away: LAL, home: BOS },
    { game_id: "0022501001", tip_time: null, away: team("NYK"), home: PHI },
  ],
};

const morning = (slate: SlatePage = SLATE) =>
  mockApi({ "/api/slate": { body: slate }, "/api/league": { body: LEAGUE_PAGE } });

/** The player's row in the side's lines table. */
const rowOf = (side: HTMLElement, name: string) =>
  within(within(side).getByRole("table")).getByRole("link", { name }).closest("tr")!;

describe("TodayTab", () => {
  it("renders each game: matchup, tip-off in Eastern and the B2B side", async () => {
    morning();
    renderApp(MORNING);

    const lalBos = await screen.findByRole("region", { name: "LAL @ BOS" });
    expect(within(lalBos).getByRole("heading", { level: 2 })).toHaveTextContent(
      "LALB2B @ BOS, 7:30 PM ET",
    );
    const nykPhi = screen.getByRole("region", { name: "NYK @ PHI" });
    expect(within(nykPhi).getByRole("heading", { level: 2 })).toHaveTextContent(
      "NYK @ PHI, time TBD",
    );
    expect(within(nykPhi).queryByText("B2B")).not.toBeInTheDocument();
  });

  it("shows each side's report line, and 'Not yet submitted' with its G League badges alone", async () => {
    morning();
    renderApp(MORNING);
    const game = await screen.findByRole("region", { name: "LAL @ BOS" });

    const bos = within(game).getByRole("region", { name: "BOS" });
    expect(bos).toHaveTextContent("Official report, Mar 21, 2026, 11:30 AM ET");
    const lal = within(game).getByRole("region", { name: "LAL" });
    expect(lal).toHaveTextContent("Not yet submitted");
    const knecht = rowOf(lal, "Dalton Knecht");
    expect(knecht).toHaveTextContent("Dalton KnechtG League—");

    const phi = within(screen.getByRole("region", { name: "NYK @ PHI" })).getByRole("region", {
      name: "PHI",
    });
    expect(phi).toHaveTextContent("ESPN, Mar 21, 2026, 9:00 AM ETstale");
    expect(phi).toHaveTextContent("Nobody listed.");
    expect(within(phi).getByText("stale")).toHaveClass("marker-stale");
  });

  it("leads each changed line with its kind, the previous status beside the new", async () => {
    morning();
    renderApp(MORNING);
    const bos = within(await screen.findByRole("region", { name: "LAL @ BOS" })).getByRole(
      "region",
      { name: "BOS" },
    );

    const cells = (name: string) =>
      within(rowOf(bos, name))
        .getAllByRole("cell")
        .map((cell) => cell.textContent);
    expect(cells("Jayson Tatum")).toEqual([
      "newJayson Tatum",
      "Out",
      "Achilles",
      "Injured",
      "50.0%",
      "+1.3DARKO",
    ]);
    expect(cells("Kristaps Porzingis").slice(0, 2)).toEqual([
      "downgradedKristaps Porzingis",
      "QuestionableProbable → Questionable",
    ]);
    expect(cells("Jaylen Brown").slice(0, 2)).toEqual([
      "upgradedJaylen Brown",
      "AvailableDoubtful → Available",
    ]);
    // Unchanged: no marker, no note.
    expect(cells("Payton Pritchard").slice(0, 2)).toEqual(["Payton Pritchard", "Probable"]);
    // Off: the previous status, struck through.
    const holiday = rowOf(bos, "Jrue Holiday");
    expect(holiday).toHaveClass("line-off");
    expect(cells("Jrue Holiday").slice(0, 2)).toEqual(["offJrue Holiday", "Questionable"]);
    expect(within(holiday).getByText("Questionable").tagName).toBe("S");

    for (const [name, kind] of [
      ["Jayson Tatum", "new"],
      ["Kristaps Porzingis", "downgraded"],
      ["Jaylen Brown", "upgraded"],
      ["Jrue Holiday", "off"],
    ] as const) {
      expect(within(rowOf(bos, name)).getByText(kind)).toHaveClass(`change change-${kind}`);
    }
  });

  it("lists the projected starters, linking each, a replacement badged", async () => {
    morning();
    renderApp(MORNING);
    const game = await screen.findByRole("region", { name: "LAL @ BOS" });

    const starters = within(within(game).getByRole("region", { name: "BOS" })).getAllByRole(
      "listitem",
    );
    expect(starters.map((li) => li.textContent)).toEqual([
      "Jaylen Brown",
      "Derrick White",
      "Jrue Holiday",
      "Kristaps Porzingis",
      "Payton Pritchardreplacement",
    ]);
    expect(within(starters[0]!).getByRole("link", { name: "Jaylen Brown" })).toHaveAttribute(
      "href",
      `/players/${BROWN.player_id}?date=2026-03-21`,
    );
  });

  it("shows only the games of the selected teams", async () => {
    morning();
    renderApp(`${MORNING}&team=PHI`);

    expect(await screen.findByRole("region", { name: "NYK @ PHI" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "LAL @ BOS" })).not.toBeInTheDocument();
  });

  it("says no selected team plays today", async () => {
    morning();
    renderApp(`${MORNING}&team=DEN`);

    expect(await screen.findByRole("tabpanel", { name: "Today" })).toHaveTextContent(
      "No games for the selected teams.",
    );
  });

  it("says a morning with no games has none, whatever the filter", async () => {
    morning({ ...SLATE, games: [] });
    renderApp(`${MORNING}&team=PHI`);

    expect(await screen.findByRole("tabpanel", { name: "Today" })).toHaveTextContent(
      "No games on 2026-03-21.",
    );
  });
});
