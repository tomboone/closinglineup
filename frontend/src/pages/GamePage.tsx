import { useMemo } from "react";
import { useParams } from "react-router";
import type { GamePage as GamePageData } from "../api/client";
import { useGame } from "../api/hooks";
import type { components } from "../api/types";
import StintTimeline from "../components/charts/StintTimeline";
import type { StintRow } from "../components/charts/StintTimeline";
import { signed } from "../components/format";
import PlayerLink, { PlayerList } from "../components/PlayerLink";
import TeamLink from "../components/TeamLink";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

type GameTeam = components["schemas"]["GameTeam"];

const REGULATION_PERIODS = 4;

const STATUS_LABELS: Record<GamePageData["status"], string> = {
  scheduled: "Scheduled",
  final: "Final",
  postponed: "Postponed",
};

/** Seconds played as the box score shows them: 2052 → "34:12". */
function minutes(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * One game: who was on the floor when, how each five did, and who closed it. A game is one date,
 * so the page follows `?garbage_time=` but not `?date=` (spec §8).
 */
function GamePage() {
  const gameId = useParams().gameId ?? "";
  const { garbageTime } = useShellParams();
  const game = useGame(gameId, { garbage_time: garbageTime });
  const page = game.data;

  return (
    <>
      <h1>{page ? `${page.away.tricode} at ${page.home.tricode}` : "Game"}</h1>
      <QueryStatus query={game}>{(data) => <GamePageBody page={data} />}</QueryStatus>
    </>
  );
}

function GamePageBody({ page }: { page: GamePageData }) {
  const { away, home } = page;
  const garbage = page.garbage_time === "include" ? "included" : "excluded";

  return (
    <>
      <p className="game-header">
        {STATUS_LABELS[page.status]}, {page.game_date}: <TeamLink tricode={away.tricode} />{" "}
        {away.score ?? "—"}, <TeamLink tricode={home.tricode} /> {home.score ?? "—"}.
      </p>
      <div className="game-page">
        <section className="game-timeline" aria-labelledby="game-timeline">
          <h2 id="game-timeline">Stints</h2>
          <p className="note">Every stint of both teams, garbage time included and shaded.</p>
          <Timeline page={page} />
          <div className="game-teams">
            <PlayersTable team={away} />
            <PlayersTable team={home} />
          </div>
        </section>
        <section aria-labelledby="game-closing">
          <h2 id="game-closing">Closing lineup</h2>
          <ClosingLineup away={away} home={home} />
        </section>
        <section aria-labelledby="game-lineups">
          <h2 id="game-lineups">Lineups</h2>
          <p className="note">Every five by possessions, garbage time {garbage}.</p>
          <GameLineupTable team={away} />
          <GameLineupTable team={home} />
        </section>
      </div>
    </>
  );
}

/** The gantt of both teams' stints, away first, a row per player in his box-score order. */
function Timeline({ page }: { page: GamePageData }) {
  const rows = useMemo(() => timelineRows(page), [page]);
  const periods = Math.max(REGULATION_PERIODS, ...page.stints.map((stint) => stint.period));
  if (page.stints.length === 0) return <p>No stints for this game yet.</p>;
  return <StintTimeline rows={rows} periods={periods} garbageTimeFrom={page.garbage_time_from} />;
}

function timelineRows(page: GamePageData): StintRow[] {
  return [page.away, page.home].flatMap((team) =>
    team.players.map(({ player }) => ({
      label: player.name ?? `Player ${player.player_id}`,
      team: team.tricode,
      stints: page.stints.filter(
        (stint) => stint.player_id === player.player_id && stint.team_id === team.team_id,
      ),
    })),
  );
}

/** A team's timeline rows as a table, each name linking to its player page. */
function PlayersTable({ team }: { team: GameTeam }) {
  return (
    <table className="game-players">
      <caption>{team.tricode}</caption>
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Min</th>
        </tr>
      </thead>
      <tbody>
        {team.players.map((line) => (
          <tr key={line.player.player_id}>
            <td>
              <PlayerLink player={line.player} />
              {line.is_starter && <span className="badge">Starter</span>}
            </td>
            <td className="number">{minutes(line.seconds)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Each team's five at the final buzzer, which the API sends only for a game within 10. */
function ClosingLineup({ away, home }: { away: GameTeam; home: GameTeam }) {
  if (away.closing == null && home.closing == null) return <p>Not a close game.</p>;
  return (
    <ul className="closing-lineups">
      {[away, home].map((team) => (
        <li key={team.team_id}>
          <strong>{team.tricode}</strong>{" "}
          {team.closing ? <PlayerList players={team.closing} /> : "—"}
        </li>
      ))}
    </ul>
  );
}

/** A team's fives in the game by offensive possessions, with the points on them and net rating. */
function GameLineupTable({ team }: { team: GameTeam }) {
  return (
    <table className="lineup-table">
      <caption>{team.tricode}</caption>
      <thead>
        <tr>
          <th scope="col">Players</th>
          <th scope="col">Poss</th>
          <th scope="col">Pts</th>
          <th scope="col">Opp poss</th>
          <th scope="col">Opp pts</th>
          <th scope="col">Net</th>
        </tr>
      </thead>
      <tbody>
        {team.lineups.length === 0 && (
          <tr>
            <td colSpan={6}>None.</td>
          </tr>
        )}
        {team.lineups.map((lineup) => (
          <tr key={lineup.players.map((player) => player.player_id).join("-")}>
            <td>
              <PlayerList players={lineup.players} />
            </td>
            <td className="number">{lineup.possessions}</td>
            <td className="number">{lineup.pts_for}</td>
            <td className="number">{lineup.opp_possessions}</td>
            <td className="number">{lineup.pts_against}</td>
            <td className="number">{signed(lineup.net_rating)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default GamePage;
