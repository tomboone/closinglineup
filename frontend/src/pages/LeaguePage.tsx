import { useId } from "react";
import type { LeaguePage as LeaguePageData, SlatePage } from "../api/client";
import { useLeague, useSlate, useTeams } from "../api/hooks";
import type { components } from "../api/types";
import SparklineCell, { type SparklineRange } from "../components/charts/SparklineCell";
import { TIER_COLOURS } from "../components/charts/theme";
import { FlagGroups } from "../components/FlagList";
import { percent, signed } from "../components/format";
import { IMPACT_LABELS, TIER_LABELS, TRANSACTION_LABELS } from "../components/labels";
import PlayerLink from "../components/PlayerLink";
import TeamLink from "../components/TeamLink";
import { dayBefore, easternToday, type Tab, TABS, useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

type Schemas = components["schemas"];
type Mover = Schemas["Mover"];

// The API's counts (`api/routers/league.py`), which the tables never exceed.
const MINUTES_MOVERS = 15;
const IMPACT_MOVERS = 10;

const TAB_LABELS: Record<Tab, string> = {
  today: "Today",
  rotation: "Rotation",
  movers: "Movers",
  flags: "Flags",
  moves: "Moves",
};

/**
 * The morning feed (v2 spec §3, §4): the morning's two dates, a tab per view and a team filter, the
 * derived tabs reading last night through `GET /api/league` and Today reading the morning's slate.
 * Last night is the slate's `last_night`, the latest derived date before the morning; every page is
 * fetched up front, so switching tabs does not refetch.
 */
function LeaguePage() {
  const { date, tab, teams, setTab, setTeams } = useShellParams();
  const morning = date ?? easternToday();
  const yesterday = dayBefore(morning);
  const slate = useSlate({ date: morning });
  const lastNight = slate.data?.last_night ?? null;
  const league = useLeague({ date: lastNight ?? undefined }, { enabled: lastNight !== null });
  // The route gives a date's transactions and the day after's, so last night's page has the
  // morning's only when last night is the day before; when it lags, the day before's page has them.
  const lagging = lastNight !== null && lastNight < yesterday;
  const dayBeforeLeague = useLeague({ date: yesterday }, { enabled: lagging });
  const morningLeague = lagging ? dayBeforeLeague : league;
  const ids = useId();
  const tabId = (one: Tab) => `${ids}-tab-${one}`;
  const panelId = `${ids}-panel`;

  return (
    <>
      <h1>League</h1>
      <QueryStatus query={slate}>
        {(page) => (
          <>
            <div className="feed-header">
              <FeedDates morning={morning} lastNight={page.last_night} />
              <div role="tablist" aria-label="Feed">
                {TABS.map((one) => (
                  <button
                    key={one}
                    type="button"
                    role="tab"
                    id={tabId(one)}
                    aria-selected={one === tab}
                    aria-controls={panelId}
                    onClick={() => setTab(one)}
                  >
                    {TAB_LABELS[one]}
                  </button>
                ))}
              </div>
              <TeamFilter selected={teams} onChange={setTeams} />
            </div>
            <div role="tabpanel" id={panelId} aria-labelledby={tabId(tab)} className="league-page">
              {tab === "today" ? (
                <TodayPanel slate={page} />
              ) : page.last_night === null ? (
                <p>No last night yet: no game date before {morning} has been derived.</p>
              ) : (
                <QueryStatus query={league}>
                  {(derived) =>
                    tab === "moves" ? (
                      <QueryStatus query={morningLeague}>
                        {(ofMorning) => (
                          <DerivedPanel
                            tab={tab}
                            page={filtered(withMoves(derived, ofMorning, morning), teams)}
                          />
                        )}
                      </QueryStatus>
                    ) : (
                      <DerivedPanel tab={tab} page={filtered(derived, teams)} />
                    )
                  }
                </QueryStatus>
              )}
            </div>
          </>
        )}
      </QueryStatus>
    </>
  );
}

/**
 * The morning's two dates: last night, saying which date is not derived yet when it lags the day
 * before the morning, and today.
 */
function FeedDates({ morning, lastNight }: { morning: string; lastNight: string | null }) {
  const yesterday = dayBefore(morning);
  return (
    <p className="feed-dates">
      <span>
        Last night: {lastNight ?? "none yet"}
        {lastNight !== null && lastNight < yesterday && ` (${yesterday} not derived yet)`}
      </span>
      <span>Today: {morning}</span>
    </p>
  );
}

/** A checkbox per tricode; none checked is every team. */
function TeamFilter({
  selected,
  onChange,
}: {
  selected: ReadonlySet<string>;
  onChange: (teams: ReadonlySet<string>) => void;
}) {
  const teams = useTeams();
  const toggle = (tricode: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(tricode);
    else next.delete(tricode);
    onChange(next);
  };
  return (
    <details className="team-filter">
      <summary>Teams: {selected.size === 0 ? "all" : [...selected].sort().join(", ")}</summary>
      <fieldset>
        <legend>Show teams</legend>
        {teams.data?.teams.map((team) => (
          <label key={team.tricode}>
            <input
              type="checkbox"
              checked={selected.has(team.tricode)}
              onChange={(event) => toggle(team.tricode, event.target.checked)}
            />{" "}
            {team.tricode}
          </label>
        ))}
        <button type="button" disabled={selected.size === 0} onClick={() => onChange(new Set())}>
          All teams
        </button>
      </fieldset>
    </details>
  );
}

/**
 * The page's rows of the selected teams, every row when none is selected: an event by its team, a
 * mover or a flag by its tricode, a transaction by either of its teams. The movers are filtered after
 * the API ranked them league-wide.
 */
function filtered(page: LeaguePageData, teams: ReadonlySet<string>): FeedPage {
  const has = (tricode: string | null) => tricode !== null && teams.has(tricode);
  const keep = <T,>(rows: T[], tricodes: (row: T) => (string | null)[]) =>
    teams.size === 0 ? rows : rows.filter((row) => tricodes(row).some(has));
  const byTricode = (row: { tricode: string | null }) => [row.tricode];
  return {
    ...page,
    // The page reads the date alone, so a date no team played has no events, movers or flags, which
    // come of its games; a transaction can still land on it (spec §8).
    noGames: [
      page.rotation_events,
      page.minutes_risers,
      page.minutes_fallers,
      page.flags,
      page.impact_risers,
      page.impact_fallers,
    ].every((list) => list.length === 0),
    rotation_events: keep(page.rotation_events, byTricode),
    minutes_risers: keep(page.minutes_risers, byTricode),
    minutes_fallers: keep(page.minutes_fallers, byTricode),
    impact_risers: keep(page.impact_risers, byTricode),
    impact_fallers: keep(page.impact_fallers, byTricode),
    flags: keep(page.flags, byTricode),
    transactions: keep(page.transactions, (row) => [row.from_tricode, row.to_tricode]),
  };
}

type FeedPage = LeaguePageData & { noGames: boolean };

/** Last night's page with its transactions narrowed to last night's and the morning's (v2 spec §3). */
function withMoves(
  lastNight: LeaguePageData,
  ofMorning: LeaguePageData,
  morning: string,
): LeaguePageData {
  return {
    ...lastNight,
    transactions: [
      ...lastNight.transactions.filter((transaction) => transaction.date === lastNight.date),
      ...ofMorning.transactions.filter((transaction) => transaction.date === morning),
    ],
  };
}

/** Today's slate; its games, reports and projected starters come with the Today tab's own ticket. */
function TodayPanel({ slate }: { slate: SlatePage }) {
  const games = slate.games.length;
  return (
    <p>
      {games === 0
        ? `No games on ${slate.date}.`
        : `${games} ${games === 1 ? "game" : "games"} on ${slate.date}.`}
    </p>
  );
}

/** One of the tabs that read last night, its page already filtered to the selected teams. */
function DerivedPanel({ tab, page }: { tab: Exclude<Tab, "today">; page: FeedPage }) {
  const empty = (emptyMessage: string) => (
    <p>{page.noGames ? `No games on ${page.date}.` : emptyMessage}</p>
  );

  switch (tab) {
    case "rotation":
      return (
        <section className="league-events" aria-labelledby="league-events">
          <h2 id="league-events">Rotation changes</h2>
          {page.rotation_events.length === 0 ? (
            empty("No rotation changes.")
          ) : (
            <RotationEvents teams={page.rotation_events} />
          )}
        </section>
      );
    case "movers":
      return (
        <>
          <section aria-labelledby="league-minutes">
            <h2 id="league-minutes">Minutes movers</h2>
            <p className="note">10-game minutes share against the 10 games before.</p>
            {page.minutes_risers.length + page.minutes_fallers.length === 0 ? (
              empty("No minutes movers.")
            ) : (
              <>
                <MoverTable caption="Rising" movers={page.minutes_risers} kind="minutes" />
                <MoverTable caption="Falling" movers={page.minutes_fallers} kind="minutes" />
              </>
            )}
          </section>
          <section aria-labelledby="league-impact">
            <h2 id="league-impact">Impact movers</h2>
            <p className="note">
              Impact against 10 team games before, in the first of EPM, DARKO and BPM with both.
            </p>
            {page.impact_risers.length + page.impact_fallers.length === 0 ? (
              empty("No impact movers.")
            ) : (
              <>
                <MoverTable caption="Rising" movers={page.impact_risers} kind="impact" />
                <MoverTable caption="Falling" movers={page.impact_fallers} kind="impact" />
              </>
            )}
          </section>
        </>
      );
    case "flags":
      return (
        <section aria-labelledby="league-flags">
          <h2 id="league-flags">Flags</h2>
          {page.flags.length === 0 ? empty("No flags raised.") : <FlagGroups flags={page.flags} />}
        </section>
      );
    case "moves":
      return (
        <section aria-labelledby="league-transactions">
          <h2 id="league-transactions">Transactions</h2>
          {page.transactions.length === 0 ? (
            <p>No transactions.</p>
          ) : (
            <Transactions transactions={page.transactions} />
          )}
        </section>
      );
  }
}

function Tier({ tier }: { tier: Schemas["Tier"] | null }) {
  if (tier === null) return <span className="tier">New</span>;
  return (
    <span className="tier" style={{ borderLeftColor: TIER_COLOURS[tier] }}>
      {TIER_LABELS[tier]}
    </span>
  );
}

/** Each team's tier transitions, from → to, with the signals that explain them. */
function RotationEvents({ teams }: { teams: Schemas["TeamRotationEvents"][] }) {
  return teams.map((team) => (
    <section key={team.tricode} aria-label={team.tricode} className="league-team">
      <h3>
        <TeamLink tricode={team.tricode} />
      </h3>
      <ul className="rotation-events">
        {team.events.map((event) => (
          <li key={event.player.player_id}>
            <PlayerLink player={event.player} /> <Tier tier={event.from_tier} /> →{" "}
            <Tier tier={event.to_tier} />
            <ul className="signals">
              {event.signals.map((signal) => (
                <li key={signal.name}>{signal.label}</li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  ));
}

/**
 * Movers one way, with their 10-game value, its change and a line from the prior window to it, the
 * table's lines sharing one y range so a bigger move is a steeper one; a minutes mover's value is a
 * share, an impact mover's a rating named by its source.
 */
function MoverTable({
  caption,
  movers,
  kind,
}: {
  caption: string;
  movers: Mover[];
  kind: "minutes" | "impact";
}) {
  const shown = movers.slice(0, kind === "minutes" ? MINUTES_MOVERS : IMPACT_MOVERS);
  if (shown.length === 0) return null;
  const minutes = kind === "minutes";
  const range = sharedRange(shown);
  return (
    <table>
      <caption>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Team</th>
          <th scope="col">{minutes ? "Share" : "Impact"}</th>
          <th scope="col">Change</th>
          <th scope="col">Trend</th>
        </tr>
      </thead>
      <tbody>
        {shown.map((mover) => (
          <tr key={mover.player.player_id}>
            <td>
              <PlayerLink player={mover.player} />
            </td>
            <td>{mover.tricode && <TeamLink tricode={mover.tricode} />}</td>
            <td className="number">
              {minutes ? percent(mover.value) : signed(mover.value)}
              {mover.source && <span className="impact-source">{IMPACT_LABELS[mover.source]}</span>}
            </td>
            <td className={`number ${mover.delta > 0 ? "trend-up" : "trend-down"}`}>
              {minutes ? `${signed(mover.delta * 100)} pts` : signed(mover.delta)}
            </td>
            <td>{mover.value !== null && <MoverSparkline value={mover.value} delta={mover.delta} range={range} />}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The lowest and highest of the movers' prior and latest values. */
function sharedRange(movers: Mover[]): SparklineRange | undefined {
  const values = movers.flatMap((mover) =>
    mover.value === null ? [] : [mover.value, mover.value - mover.delta],
  );
  return values.length === 0
    ? undefined
    : { min: Math.min(...values), max: Math.max(...values) };
}

// The API gives a mover's two windows, not a series: the line runs from the prior one to the latest.
function MoverSparkline({
  value,
  delta,
  range,
}: {
  value: number;
  delta: number;
  range: SparklineRange | undefined;
}) {
  return (
    <SparklineCell
      series={[
        { game_date: "prior 10", value: value - delta },
        { game_date: "last 10", value },
      ]}
      range={range}
    />
  );
}

/** Last night's and the morning's transactions, each dated, with the teams it moved the player between. */
function Transactions({ transactions }: { transactions: Schemas["TransactionSummary"][] }) {
  return (
    <ul className="league-list">
      {transactions.map((transaction, i) => (
        <li key={`${transaction.player.player_id}-${i}`}>
          <span className="flag-date">{transaction.date}</span>{" "}
          <PlayerLink player={transaction.player} />
          <span className="badge">
            {TRANSACTION_LABELS[transaction.type] ?? transaction.type}
          </span>{" "}
          {transaction.from_tricode && <TeamLink tricode={transaction.from_tricode} />}
          {transaction.from_tricode && transaction.to_tricode && " → "}
          {transaction.to_tricode && <TeamLink tricode={transaction.to_tricode} />}{" "}
          <span className="note">{transaction.detail}</span>
        </li>
      ))}
    </ul>
  );
}

export default LeaguePage;
