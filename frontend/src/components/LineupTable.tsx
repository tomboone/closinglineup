import type { components } from "../api/types";
import { signed } from "./format";
import { PlayerList } from "./PlayerLink";

type LineupStats = components["schemas"]["LineupStats"];

/** Lineups or pairs by possessions, with their season net rating and the last 10 games' trend. */
function LineupTable({ caption, rows }: { caption: string; rows: LineupStats[] }) {
  return (
    <table className="lineup-table">
      <caption>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">Players</th>
          <th scope="col">Poss</th>
          <th scope="col">Net</th>
          <th scope="col">Last 10</th>
          <th scope="col">Trend</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={5}>None yet.</td>
          </tr>
        )}
        {rows.map((row) => (
          <tr key={row.players.map((player) => player.player_id).join("-")}>
            <td>
              <PlayerList players={row.players} />
            </td>
            <td className="number">{row.possessions}</td>
            <td className="number">{signed(row.net_rating)}</td>
            <td className="number">{signed(row.net_rating_10)}</td>
            <td>
              <Trend season={row.net_rating} recent={row.net_rating_10} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Whether the last 10 games' net rating is above or below the season's. */
function Trend({ season, recent }: { season: number | null; recent: number | null }) {
  if (season == null || recent == null || recent === season) return null;
  return recent > season ? (
    <span className="trend trend-up" aria-label="Up over the last 10 games">
      ▲
    </span>
  ) : (
    <span className="trend trend-down" aria-label="Down over the last 10 games">
      ▼
    </span>
  );
}

export default LineupTable;
