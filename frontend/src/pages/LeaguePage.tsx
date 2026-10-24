import type { LeaguePage as LeaguePageData } from "../api/client";
import { useLeague } from "../api/hooks";
import type { components } from "../api/types";
import SparklineCell, { type SparklineRange } from "../components/charts/SparklineCell";
import { TIER_COLOURS } from "../components/charts/theme";
import { FlagGroups } from "../components/FlagList";
import { percent, signed } from "../components/format";
import { IMPACT_LABELS, TIER_LABELS, TRANSACTION_LABELS } from "../components/labels";
import PlayerLink from "../components/PlayerLink";
import TeamLink from "../components/TeamLink";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

type Schemas = components["schemas"];
type Mover = Schemas["Mover"];

// The API's counts (`api/routers/league.py`), which the tables never exceed.
const MINUTES_MOVERS = 15;
const IMPACT_MOVERS = 10;

/** The morning feed: what changed across the league on the date (spec §8). */
function LeaguePage() {
  const { date } = useShellParams();
  const league = useLeague({ date });

  return (
    <>
      <h1>League</h1>
      <QueryStatus query={league}>{(page) => <LeaguePageBody page={page} />}</QueryStatus>
    </>
  );
}

function LeaguePageBody({ page }: { page: LeaguePageData }) {
  // The page reads the date alone, so a date no team played has no events, movers or flags, which
  // come of its games; an injury report or a transaction can still land on it (spec §8).
  const noGames = [
    page.rotation_events,
    page.minutes_risers,
    page.minutes_fallers,
    page.flags,
    page.impact_risers,
    page.impact_fallers,
  ].every((list) => list.length === 0);
  const empty = (emptyMessage: string) => (
    <p>{noGames ? `No games on ${page.date}.` : emptyMessage}</p>
  );

  return (
    <>
      <p>Morning feed for {page.date}.</p>
      <div className="league-page">
        <section className="league-events" aria-labelledby="league-events">
          <h2 id="league-events">Rotation changes</h2>
          {page.rotation_events.length === 0 ? (
            empty("No rotation changes.")
          ) : (
            <RotationEvents teams={page.rotation_events} />
          )}
        </section>
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
        <section aria-labelledby="league-flags">
          <h2 id="league-flags">Flags</h2>
          {page.flags.length === 0 ? empty("No flags raised.") : <FlagGroups flags={page.flags} />}
        </section>
        <section aria-labelledby="league-injuries">
          <h2 id="league-injuries">Injury changes</h2>
          {page.injury_changes.length === 0 ? (
            <p>No injury changes.</p>
          ) : (
            <InjuryChanges changes={page.injury_changes} />
          )}
        </section>
        <section aria-labelledby="league-transactions">
          <h2 id="league-transactions">Transactions</h2>
          {page.transactions.length === 0 ? (
            <p>No transactions.</p>
          ) : (
            <Transactions transactions={page.transactions} />
          )}
        </section>
      </div>
    </>
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

const status = (value: Schemas["InjuryStatus"] | null, none: string) => value ?? none;

/** Players newly Out or Doubtful, or back from either, with the report that said so. */
function InjuryChanges({ changes }: { changes: Schemas["InjuryChange"][] }) {
  return (
    <ul className="league-list">
      {changes.map((change) => (
        <li key={`${change.tricode}-${change.player.player_id}`}>
          <PlayerLink player={change.player} /> <TeamLink tricode={change.tricode} />{" "}
          {status(change.from_status, "Not listed")} → {status(change.to_status, "Off the report")}
          {change.reason && <span className="note"> {change.reason}</span>}
        </li>
      ))}
    </ul>
  );
}

/** The date's transactions, each with the teams it moved the player between. */
function Transactions({ transactions }: { transactions: Schemas["TransactionSummary"][] }) {
  return (
    <ul className="league-list">
      {transactions.map((transaction, i) => (
        <li key={`${transaction.player.player_id}-${i}`}>
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
