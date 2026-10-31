import { useId } from "react";
import { Link, useLocation } from "react-router";
import type { LeaguePage as LeaguePageData } from "../api/client";
import { useLeague, useSlate, useTeams } from "../api/hooks";
import type { components } from "../api/types";
import MoverChart from "../components/charts/MoverChart";
import { TIER_COLOURS } from "../components/charts/theme";
import { FlagGroups } from "../components/FlagList";
import { percent, signed } from "../components/format";
import ImpactValue from "../components/ImpactValue";
import { IMPACT_LABELS, TIER_LABELS, TRANSACTION_LABELS } from "../components/labels";
import PlayerLink from "../components/PlayerLink";
import TabList from "../components/TabList";
import { tabIds } from "../components/tabIds";
import TeamFilter from "../components/TeamFilter";
import TeamLink from "../components/TeamLink";
import { dayBefore, easternToday, type Tab, TABS, useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";
import TodayTab from "./TodayTab";

type Schemas = components["schemas"];
type Mover = Schemas["Mover"];
type TeamSummary = Schemas["TeamSummary"];

// The API's counts (`api/routers/league.py`), which the charts and tables never exceed.
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
  last_night: "Last night",
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
  const { tab: tabId, panel: panelId } = tabIds(ids);

  return (
    <>
      <h1>League</h1>
      <QueryStatus query={slate}>
        {(page) => (
          <>
            <div className="feed-header">
              <FeedDates morning={morning} lastNight={page.last_night} />
              <TabList
                label="Feed"
                prefix={ids}
                tabs={TABS}
                labels={TAB_LABELS}
                selected={tab}
                onSelect={setTab}
              />
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

/**
 * The page's rows of the selected teams, every row when none is selected: a game or a
 * transaction by either of its teams, an event by its team, a quiet signal, a mover or a flag by
 * its tricode. The movers are filtered after the API ranked them league-wide.
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
      page.games,
      page.rotation_events,
      page.quiet_signals,
      page.minutes_risers,
      page.minutes_fallers,
      page.flags,
      page.impact_risers,
      page.impact_fallers,
    ].every((list) => list.length === 0),
    games: keep(page.games, (game) => [game.home.tricode, game.away.tricode]),
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
  // The chart and the tables beneath it draw the same movers, the API's counts at most.
  const minutesRisers = page.minutes_risers.slice(0, MINUTES_MOVERS);
  const minutesFallers = page.minutes_fallers.slice(0, MINUTES_MOVERS);
  const impactRisers = page.impact_risers.slice(0, IMPACT_MOVERS);
  const impactFallers = page.impact_fallers.slice(0, IMPACT_MOVERS);

  switch (tab) {
    case "last_night":
      return (
        <section aria-labelledby="league-games">
          <h2 id="league-games">Final scores</h2>
          <p className="note">Each side&apos;s two highest Game Scores.</p>
          {page.games.length === 0 ? (
            empty("No games for the selected teams.")
          ) : (
            <LastNightGames games={page.games} />
          )}
        </section>
      );
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
            {minutesRisers.length + minutesFallers.length === 0 ? (
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
                <MoverChart
                  risers={minutesRisers}
                  fallers={minutesFallers}
                  format={percent}
                  unitLabel="Minutes share, pts"
                  scale={100}
                />
                <MoverTable caption="Rising" movers={minutesRisers} kind="minutes" />
                <MoverTable caption="Falling" movers={minutesFallers} kind="minutes" />
              </>
            )}
          </section>
          <section aria-labelledby="league-impact">
            <h2 id="league-impact">Impact movers</h2>
            <p className="note">
              Impact against 10 team games before, in the first of EPM, DARKO and BPM with both.
            </p>
            {impactRisers.length + impactFallers.length === 0 ? (
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
                <MoverChart
                  risers={impactRisers}
                  fallers={impactFallers}
                  format={signed}
                  unitLabel={impactUnit([...impactRisers, ...impactFallers])}
                />
                <MoverTable caption="Rising" movers={impactRisers} kind="impact" />
                <MoverTable caption="Falling" movers={impactFallers} kind="impact" />
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

/**
 * Last night's final games, a row per game linking to its game page, each side's score and its two
 * highest Game Scores with the box line behind them.
 */
function LastNightGames({ games }: { games: Schemas["LastNightGame"][] }) {
  const { search } = useLocation();
  return (
    <table className="last-night">
      <thead>
        <tr>
          <th scope="col">Final</th>
          <th scope="col">Away</th>
          <th scope="col">Home</th>
        </tr>
      </thead>
      <tbody>
        {games.map((game) => (
          <tr key={game.game_id}>
            <th scope="row">
              <Link to={{ pathname: `/games/${game.game_id}`, search }}>
                {game.away.tricode} {game.away.score ?? "–"} @ {game.home.tricode}{" "}
                {game.home.score ?? "–"}
              </Link>
              {game.margin !== null && game.margin !== 0 && (
                <span className="note">
                  {" "}
                  {game.margin > 0 ? game.home.tricode : game.away.tricode} by{" "}
                  {Math.abs(game.margin)}
                </span>
              )}
            </th>
            <td>
              <TopLines lines={game.away.top} />
            </td>
            <td>
              <TopLines lines={game.home.top} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A side's top Game Scores, the highest first, with points, rebounds, assists and minutes. */
function TopLines({ lines }: { lines: Schemas["GameScoreLine"][] }) {
  if (lines.length === 0) return null;
  return (
    <ol className="top-lines">
      {lines.map((line) => (
        <li key={line.player.player_id}>
          <PlayerLink player={line.player} />{" "}
          <span className="number">{line.game_score.toFixed(1)}</span>{" "}
          <span className="note">
            {line.pts} pts, {line.reb} reb, {line.ast} ast, {Math.round(line.seconds / 60)} min
          </span>
        </li>
      ))}
    </ol>
  );
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
 * Movers one way, each with his impact beside him, with their 10-game value and its change, the
 * exact numbers beneath the chart's bars; a minutes mover's value is a share, an impact mover's an
 * impact value named by its source.
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
  if (movers.length === 0) return null;
  const minutes = kind === "minutes";
  return (
    <table>
      <caption>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Team</th>
          <th scope="col">{minutes ? "Share" : "Impact"}</th>
          <th scope="col">Change</th>
        </tr>
      </thead>
      <tbody>
        {movers.map((mover) => (
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
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The impact movers' axis unit: the rating of the source every mover shares ("DARKO rating"), or
 * "Impact rating" when their sources differ.
 */
function impactUnit(movers: Mover[]) {
  const sources = new Set(movers.map((mover) => mover.source));
  const [source] = sources;
  return sources.size === 1 && source ? `${IMPACT_LABELS[source]} rating` : "Impact rating";
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
