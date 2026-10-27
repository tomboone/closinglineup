import type { ReactNode } from "react";
import { useSearchParams } from "react-router";
import type { LeagueTable } from "../api/client";
import { useLeagueTable, useTeams } from "../api/hooks";
import type { components } from "../api/types";
import { DASH, decimal, percent, ratio } from "../components/format";
import ImpactValue from "../components/ImpactValue";
import { TIER_LABELS } from "../components/labels";
import PlayerLink from "../components/PlayerLink";
import TeamFilter from "../components/TeamFilter";
import TeamLink from "../components/TeamLink";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

type Schemas = components["schemas"];
type Row = LeagueTable["rows"][number];
type Window = Schemas["LeagueWindow"];
type Order = "asc" | "desc";

// The API's default window and the page's defaults, which the URL leaves unset.
const DEFAULT_WINDOW: Window = "season";
const DEFAULT_SORT = "ws_48";
const DEFAULT_MIN_MPG = 15;

const WINDOWS: { window: Window; label: string }[] = [
  { window: "5", label: "Last 5" },
  { window: "10", label: "Last 10" },
  { window: "20", label: "Last 20" },
  { window: "season", label: "Season" },
  { window: "prior_season", label: "Prior season" },
];

// A tier's place when sorting by it, the highest first.
const TIER_RANK: Record<Schemas["Tier"], number> = { starter: 4, core: 3, fringe: 2, out: 1, injured: 0 };

type Column = {
  key: string;
  label: string;
  title?: string;
  numeric: boolean;
  value: (row: Row) => number | string | null;
  render: (row: Row) => ReactNode;
};

const COLUMNS: Column[] = [
  {
    key: "player",
    label: "Player",
    numeric: false,
    value: (row) => row.player.name ?? `Player ${row.player.player_id}`,
    render: (row) => <PlayerLink player={row.player} />,
  },
  {
    key: "tricode",
    label: "Team",
    numeric: false,
    value: (row) => row.tricode,
    render: (row) => (row.tricode === null ? DASH : <TeamLink tricode={row.tricode} />),
  },
  {
    key: "tier",
    label: "Tier",
    numeric: false,
    value: (row) => (row.tier === null ? null : TIER_RANK[row.tier]),
    render: (row) => (row.tier === null ? DASH : TIER_LABELS[row.tier]),
  },
  {
    key: "games_played",
    label: "G",
    title: "Games played of his teams' games in the window",
    numeric: true,
    value: (row) => row.games_played,
    render: (row) => `${row.games_played} of ${row.games}`,
  },
  {
    key: "minutes_per_game",
    label: "MPG",
    numeric: true,
    value: (row) => row.minutes_per_game,
    render: (row) => decimal(row.minutes_per_game),
  },
  {
    key: "impact",
    label: "Impact",
    numeric: true,
    value: (row) => row.impact?.value ?? null,
    render: (row) => <ImpactValue impact={row.impact} />,
  },
  { key: "ws", label: "WS", numeric: true, value: (row) => row.ws, render: (row) => decimal(row.ws) },
  { key: "ws_48", label: "WS/48", numeric: true, value: (row) => row.ws_48, render: (row) => ratio(row.ws_48) },
  {
    key: "vorp",
    label: "VORP",
    title: "Season and prior season only",
    numeric: true,
    value: (row) => row.vorp,
    render: (row) => decimal(row.vorp),
  },
  { key: "ts_pct", label: "TS%", numeric: true, value: (row) => row.ts_pct, render: (row) => percent(row.ts_pct) },
  { key: "usg_pct", label: "USG%", numeric: true, value: (row) => row.usg_pct, render: (row) => percent(row.usg_pct) },
  {
    key: "game_score",
    label: "Game Score",
    numeric: true,
    value: (row) => row.game_score,
    render: (row) => decimal(row.game_score),
  },
];

/**
 * The page's parameters, kept in the URL beside the shell's: `?window=` (unset means the season;
 * the player page shares it), `?sort=` (unset means WS/48) with `&order=asc` when ascending,
 * `?min=` minutes per game (unset means 15) and `?min_games=` (unset means half each player's
 * team games in the window, rounded up). The shell's `?team=` is the feed's team filter.
 */
function useTableParams() {
  const [searchParams, setSearchParams] = useSearchParams();

  const set = (values: Record<string, string | undefined>) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      for (const [name, value] of Object.entries(values)) {
        if (value === undefined) next.delete(name);
        else next.set(name, value);
      }
      return next;
    });
  };

  const param = searchParams.get("window");
  const window = WINDOWS.find((w) => w.window === param)?.window ?? DEFAULT_WINDOW;
  const sortParam = searchParams.get("sort");
  const sort = COLUMNS.find((column) => column.key === sortParam)?.key ?? DEFAULT_SORT;
  const order: Order = searchParams.get("order") === "asc" ? "asc" : "desc";
  // The inputs' text as typed, so clearing one leaves it empty rather than snapping back.
  const minText = searchParams.get("min") ?? String(DEFAULT_MIN_MPG);
  const minGamesText = searchParams.get("min_games") ?? "";

  return {
    window,
    sort,
    order,
    minText,
    minGamesText,
    setWindow: (value: Window) => set({ window: value === DEFAULT_WINDOW ? undefined : value }),
    // A new column sorts descending; the sorted one flips.
    setSort: (key: string) => {
      const flipped: Order = key === sort && order === "desc" ? "asc" : "desc";
      set({ sort: key === DEFAULT_SORT ? undefined : key, order: flipped === "asc" ? "asc" : undefined });
    },
    setMin: (text: string) => set({ min: text === String(DEFAULT_MIN_MPG) ? undefined : text }),
    setMinGames: (text: string) => set({ min_games: text === "" ? undefined : text }),
  };
}

/**
 * The league table (NEU-2006): every player's latest production row in the window, sortable by any
 * column and filtered by team and by minimum minutes and games. The API sends every row unsorted,
 * so sorting and filtering need no refetch.
 */
function PlayersPage() {
  const { date, teams, setTeams } = useShellParams();
  const params = useTableParams();
  const table = useLeagueTable({
    date,
    window: params.window === DEFAULT_WINDOW ? undefined : params.window,
  });
  const summaries = useTeams().data?.teams;

  return (
    <>
      <h1>Players</h1>
      <div className="players-controls">
        <WindowToggle window={params.window} onChange={params.setWindow} />
        <label>
          Min MPG{" "}
          <input
            type="number"
            min={0}
            value={params.minText}
            onChange={(event) => params.setMin(event.target.value)}
          />
        </label>
        <label>
          Min games{" "}
          <input
            type="number"
            min={0}
            placeholder="half"
            value={params.minGamesText}
            onChange={(event) => params.setMinGames(event.target.value)}
          />
        </label>
        <TeamFilter teams={summaries} selected={teams} onChange={setTeams} />
      </div>
      <QueryStatus query={table}>
        {(page) => (
          <PlayersTable
            rows={page.rows}
            teams={teams}
            minMpg={Number(params.minText) || 0}
            minGames={params.minGamesText === "" ? null : Number(params.minGamesText) || 0}
            sort={params.sort}
            order={params.order}
            onSort={params.setSort}
          />
        )}
      </QueryStatus>
    </>
  );
}

function WindowToggle({ window, onChange }: { window: Window; onChange: (window: Window) => void }) {
  return (
    <fieldset className="window-toggle">
      <legend>Window</legend>
      {WINDOWS.map((w) => (
        <label key={w.window}>
          <input
            type="radio"
            name="window"
            value={w.window}
            checked={w.window === window}
            onChange={() => onChange(w.window)}
          />
          {w.label}
        </label>
      ))}
    </fieldset>
  );
}

/** Ascending order of two values, a null after any value whichever the order. */
function compare(a: number | string | null, b: number | string | null, order: Order) {
  if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1;
  const ascending = typeof a === "string" && typeof b === "string" ? a.localeCompare(b) : Number(a) - Number(b);
  return order === "asc" ? ascending : -ascending;
}

function PlayersTable({
  rows,
  teams,
  minMpg,
  minGames,
  sort,
  order,
  onSort,
}: {
  rows: Row[];
  teams: ReadonlySet<string>;
  minMpg: number;
  // Null is half each player's team games in the window, rounded up.
  minGames: number | null;
  sort: string;
  order: Order;
  onSort: (key: string) => void;
}) {
  const onTeams = rows.filter((row) => teams.size === 0 || (row.tricode !== null && teams.has(row.tricode)));
  const enoughMinutes = (row: Row) => (row.minutes_per_game ?? 0) >= minMpg;
  const enoughGames = (row: Row) => row.games_played >= (minGames ?? Math.ceil(row.games / 2));
  const qualified = onTeams.filter((row) => enoughMinutes(row) && enoughGames(row));
  const column = COLUMNS.find((c) => c.key === sort) ?? COLUMNS[0];
  const sorted = [...qualified].sort((a, b) => compare(column.value(a), column.value(b), order));

  if (onTeams.length === 0) return <p>No players in the window.</p>;
  if (sorted.length === 0) {
    const lower = [
      onTeams.some((row) => !enoughMinutes(row)) && "minutes per game",
      onTeams.some((row) => !enoughGames(row)) && "games",
    ].filter(Boolean);
    return <p>No player qualifies: lower the minimum {lower.join(" or the minimum ")}.</p>;
  }
  return (
    <>
      <p className="note">
        {sorted.length} {sorted.length === 1 ? "player qualifies" : "players qualify"}
      </p>
      <table className="players-table">
        <thead>
          <tr>
            {COLUMNS.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={c.numeric ? "number" : undefined}
                aria-sort={c.key === sort ? (order === "asc" ? "ascending" : "descending") : undefined}
              >
                <button type="button" title={c.title} onClick={() => onSort(c.key)}>
                  {c.label}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.player.player_id}>
              {COLUMNS.map((c) => (
                <td key={c.key} className={c.numeric ? "number" : undefined}>
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export default PlayersPage;
