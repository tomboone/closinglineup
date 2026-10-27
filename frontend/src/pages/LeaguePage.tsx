import { useId } from "react";
import type { LeaguePage as LeaguePageData } from "../api/client";
import { useLeague, useSlate, useTeams } from "../api/hooks";
import type { components } from "../api/types";
import SparklineCell, { type SparklineRange } from "../components/charts/SparklineCell";
import { TIER_COLOURS } from "../components/charts/theme";
import { FlagGroups } from "../components/FlagList";
import { percent, signed } from "../components/format";
import ImpactValue from "../components/ImpactValue";
import { IMPACT_LABELS, TIER_LABELS, TRANSACTION_LABELS } from "../components/labels";
import PlayerLink from "../components/PlayerLink";
import TeamLink from "../components/TeamLink";
import { dayBefore, easternToday, type Tab, TABS, useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";
import TodayTab from "./TodayTab";

type Schemas = components["schemas"];
type Mover = Schemas["Mover"];
type TeamSummary = Schemas["TeamSummary"];

// The API's counts (`api/routers/league.py`), which the tables never exceed.
const MINUTES_MOVERS = 15;
const IMPACT_MOVERS = 10;
// The team games each empty list needs before it can fill: a minutes mover's 10-game window against
// the 10 before it (`player_window_stats`' `_delta_prior`), an impact mover's value 10 games back, and
// the flags' longest window, minutes cliff's 20 games.
const MINUTES_MOVER_GAMES = 20;
const IMPACT_MOVER_GAMES = 11;
const FLAG_GAMES = 20;

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
  const summaries = useTeams().data?.teams;
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
              <TeamFilter teams={summaries} selected={teams} onChange={setTeams} />
            </div>
            <div role="tabpanel" id={panelId} aria-labelledby={tabId(tab)} className="league-page">
              {tab === "today" ? (
                <TodayTab slate={page} teams={teams} />
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
                            games={played(summaries, teams)}
                          />
                        )}
                      </QueryStatus>
                    ) : (
                      <DerivedPanel
                        tab={tab}
                        page={filtered(derived, teams)}
                        games={played(summaries, teams)}
                      />
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
  teams,
  selected,
  onChange,
}: {
  teams: TeamSummary[] | undefined;
  selected: ReadonlySet<string>;
  onChange: (teams: ReadonlySet<string>) => void;
}) {
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
        {teams?.map((team) => (
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
 * quiet signal, a mover or a flag by its tricode, a transaction by either of its teams. The movers
 * are filtered after the API ranked them league-wide.
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
      page.quiet_signals,
      page.minutes_risers,
      page.minutes_fallers,
      page.flags,
      page.impact_risers,
      page.impact_fallers,
    ].every((list) => list.length === 0),
    rotation_events: keep(page.rotation_events, byTricode),
    quiet_signals: keep(page.quiet_signals, byTricode),
    minutes_risers: keep(page.minutes_risers, byTricode),
    minutes_fallers: keep(page.minutes_fallers, byTricode),
    impact_risers: keep(page.impact_risers, byTricode),
    impact_fallers: keep(page.impact_fallers, byTricode),
    flags: keep(page.flags, byTricode),
    transactions: keep(page.transactions, (row) => [row.from_tricode, row.to_tricode]),
  };
}

type FeedPage = LeaguePageData & { noGames: boolean };

/** The regular-season games the selected teams, or the league, have played: who, and how many. */
type Played = { who: string; plural: boolean; least: number; most: number };

/**
 * The selected teams' games, the league's when none is selected; null before the teams have loaded
 * or when no selected team is one of them. A team's games are its wins and losses (`GET /api/teams`).
 */
function played(summaries: TeamSummary[] | undefined, selected: ReadonlySet<string>): Played | null {
  const rows =
    summaries?.filter((team) => selected.size === 0 || selected.has(team.tricode)) ?? [];
  if (rows.length === 0) return null;
  const counts = rows.map((team) => team.wins + team.losses);
  return {
    who: selected.size === 0 ? "the league" : rows.map((team) => team.tricode).join(", "),
    plural: selected.size > 0 && rows.length > 1,
    least: Math.min(...counts),
    most: Math.max(...counts),
  };
}

/**
 * An empty list's message: while no counted team has played the `needs` games its window takes,
 * `window`, the window it needs, and the games played ("BOS has played 3.", a range when they
 * differ); once one has, the list is empty for the date alone and says `none`.
 */
function emptyList(none: string, window: string, needs: number, games: Played | null) {
  if (games === null) return window;
  if (games.most >= needs) return none;
  const n = games.least === games.most ? `${games.least}` : `${games.least}–${games.most}`;
  const who = `${games.who[0]!.toUpperCase()}${games.who.slice(1)}`;
  return `${window} ${who} ${games.plural ? "have" : "has"} played ${n}.`;
}

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

/**
 * One of the tabs that read last night, its page already filtered to the selected teams. An empty
 * movers or flags list early in the season says what window it needs and how many `games` the
 * selected teams have played, so the morning reads as early rather than broken (v2 spec §2).
 */
function DerivedPanel({
  tab,
  page,
  games,
}: {
  tab: Exclude<Tab, "today">;
  page: FeedPage;
  games: Played | null;
}) {
  const empty = (emptyMessage: string) => (
    <p>{page.noGames ? `No games on ${page.date}.` : emptyMessage}</p>
  );

  switch (tab) {
    case "rotation":
      return (
        <>
          <section className="league-events" aria-labelledby="league-events">
            <h2 id="league-events">Rotation changes</h2>
            {page.rotation_events.length === 0 ? (
              empty("No rotation changes.")
            ) : (
              <RotationEvents teams={page.rotation_events} />
            )}
          </section>
          <section className="league-quiet" aria-labelledby="league-quiet">
            <h2 id="league-quiet">Quiet signals</h2>
            <p className="note">
              Starters and Core players under 60% of their 10-game minutes, or sat by coach&apos;s
              decision, with no rotation change yet.
            </p>
            {page.quiet_signals.length === 0 ? (
              empty("No quiet signals.")
            ) : (
              <QuietSignals signals={page.quiet_signals} />
            )}
          </section>
        </>
      );
    case "movers":
      return (
        <>
          <section aria-labelledby="league-minutes">
            <h2 id="league-minutes">Minutes movers</h2>
            <p className="note">10-game minutes share against the 10 games before.</p>
            {page.minutes_risers.length + page.minutes_fallers.length === 0 ? (
              empty(
                emptyList(
                  "No minutes movers.",
                  `Minutes movers need ${MINUTES_MOVER_GAMES} team games.`,
                  MINUTES_MOVER_GAMES,
                  games,
                ),
              )
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
              empty(
                emptyList(
                  "No impact movers.",
                  "Impact movers need an impact value 10 games back.",
                  IMPACT_MOVER_GAMES,
                  games,
                ),
              )
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
          {page.flags.length === 0 ? (
            empty(
              emptyList(
                "No flags raised.",
                "No flags raised. Earning trust needs 15 games of a spell; minutes cliff needs the " +
                  "5- and 20-game windows to differ.",
                FLAG_GAMES,
                games,
              ),
            )
          ) : (
            <FlagGroups flags={page.flags} />
          )}
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

/**
 * Each team's tier transitions, from → to, with the player's impact and the signals that explain
 * them.
 */
function RotationEvents({ teams }: { teams: Schemas["TeamRotationEvents"][] }) {
  return teams.map((team) => (
    <section key={team.tricode} aria-label={team.tricode} className="league-team">
      <h3>
        <TeamLink tricode={team.tricode} />
      </h3>
      <ul className="rotation-events">
        {team.events.map((event) => (
          <li key={event.player.player_id}>
            <PlayerLink player={event.player} /> <ImpactValue impact={event.impact} />{" "}
            <Tier tier={event.from_tier} /> →{" "}
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
 * Last night's quiet signals in the API's order, a DNP-CD first and then the largest shortfall: each
 * player with his impact, team and tier, and his minutes against his 10-game average.
 */
function QuietSignals({ signals }: { signals: Schemas["QuietSignal"][] }) {
  return (
    <ul className="league-list">
      {signals.map((signal) => (
        <li key={`${signal.tricode}-${signal.player.player_id}`}>
          <PlayerLink player={signal.player} /> <ImpactValue impact={signal.impact} />{" "}
          <TeamLink tricode={signal.tricode} /> <Tier tier={signal.tier} />{" "}
          <span className="note">
            {signal.dnp_reason === "cd"
              ? "DNP-CD"
              : `${Math.round(signal.seconds / 60)} min vs ${signal.minutes_per_game_10.toFixed(1)} avg`}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * Movers one way, each with his impact beside him, with their 10-game value, its change and a line
 * from the prior window to it, the table's lines sharing one y range so a bigger move is a steeper
 * one; a minutes mover's value is a share, an impact mover's an impact value named by its source.
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
              <PlayerLink player={mover.player} /> <ImpactValue impact={mover.impact} />
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

/**
 * Last night's and the morning's transactions, each dated, with the player's impact and the teams it
 * moved him between.
 */
function Transactions({ transactions }: { transactions: Schemas["TransactionSummary"][] }) {
  return (
    <ul className="league-list">
      {transactions.map((transaction, i) => (
        <li key={`${transaction.player.player_id}-${i}`}>
          <span className="flag-date">{transaction.date}</span>{" "}
          <PlayerLink player={transaction.player} /> <ImpactValue impact={transaction.impact} />
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
