import type { SlatePage } from "../api/client";
import type { components } from "../api/types";
import { ReportLine } from "../components/AvailabilityBoard";
import { TIER_COLOURS } from "../components/charts/theme";
import { percent, signed } from "../components/format";
import { IMPACT_LABELS, TIER_LABELS } from "../components/labels";
import PlayerLink from "../components/PlayerLink";
import TeamLink from "../components/TeamLink";

type Schemas = components["schemas"];
type SlateTeam = Schemas["SlateTeam"];
type SlateLine = Schemas["SlateLine"];

const TIP_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  timeStyle: "short",
});

/**
 * The morning's slate (v2 spec §4.1, §5): a card per game of the selected teams, every game when none
 * is selected, with each side's newest report, its lines and the change on each since the previous
 * report, and its projected starters.
 */
function TodayTab({ slate, teams }: { slate: SlatePage; teams: ReadonlySet<string> }) {
  if (slate.games.length === 0) return <p>No games on {slate.date}.</p>;
  const games =
    teams.size === 0
      ? slate.games
      : slate.games.filter((game) => teams.has(game.away.tricode) || teams.has(game.home.tricode));
  if (games.length === 0) return <p>No games for the selected teams.</p>;
  return games.map((game) => {
    const matchup = `${game.away.tricode} @ ${game.home.tricode}`;
    return (
      <section key={game.game_id} aria-label={matchup} className="slate-game">
        <h2>
          <Side team={game.away} /> @ <Side team={game.home} />,{" "}
          {game.tip_time === null ? "time TBD" : `${TIP_TIME.format(new Date(game.tip_time))} ET`}
        </h2>
        <div className="slate-teams">
          <SlateSide team={game.away} />
          <SlateSide team={game.home} />
        </div>
      </section>
    );
  });
}

/** A side's tricode, marked when it is the team's second night in a row. */
function Side({ team }: { team: SlateTeam }) {
  return (
    <>
      {team.tricode}
      {team.b2b && (
        <span className="marker marker-b2b" title="Second night of a back-to-back">
          B2B
        </span>
      )}
    </>
  );
}

/** One team's column: its report, its lines and its projected starters. */
function SlateSide({ team }: { team: SlateTeam }) {
  return (
    <section aria-label={team.tricode}>
      <h3>
        <TeamLink tricode={team.tricode} />
      </h3>
      <ReportLine report={team.report} none="Not yet submitted" />
      {team.lines.length > 0 && <Lines lines={team.lines} />}
      <h4>Projected starters</h4>
      {team.projected_starters.length === 0 ? (
        <p>No starter rates yet.</p>
      ) : (
        <ol className="projected-starters">
          {team.projected_starters.map((starter) => (
            <li key={starter.player.player_id}>
              <PlayerLink player={starter.player} />
              {starter.replacement && (
                <span className="badge" title="In for a player ranked above him who is out">
                  replacement
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/**
 * The report's lines in the API's order, each led by its change since the previous report; an `off`
 * line, no longer listed, shows its previous status struck through.
 */
function Lines({ lines }: { lines: SlateLine[] }) {
  return (
    <table className="slate-lines">
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Status</th>
          <th scope="col">Reason</th>
          <th scope="col">Tier</th>
          <th scope="col">10-game share</th>
          <th scope="col">Impact</th>
        </tr>
      </thead>
      <tbody>
        {lines.map((line) => (
          <tr key={line.player.player_id} className={line.change === "off" ? "line-off" : undefined}>
            <td>
              {line.change && <span className={`change change-${line.change}`}>{line.change}</span>}
              <PlayerLink player={line.player} />
              {line.g_league && <span className="badge">G League</span>}
            </td>
            <td>
              <Status line={line} />
            </td>
            <td>{line.reason ?? ""}</td>
            <td>
              {line.tier === null ? (
                "—"
              ) : (
                <span className="tier" style={{ borderLeftColor: TIER_COLOURS[line.tier] }}>
                  {TIER_LABELS[line.tier]}
                </span>
              )}
            </td>
            <td className="number">{percent(line.minutes_share_10)}</td>
            <td className="number">
              {signed(line.impact?.value)}
              {line.impact && (
                <span className="impact-source">{IMPACT_LABELS[line.impact.source]}</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The line's status, with the previous report's beside it when it changed. */
function Status({ line }: { line: SlateLine }) {
  if (line.change === "off") return <s>{line.previous_status}</s>;
  return (
    <>
      {line.status ?? "—"}
      {line.change !== null && line.previous_status !== null && (
        <span className="note status-change">
          {line.previous_status} → {line.status}
        </span>
      )}
    </>
  );
}

export default TodayTab;
