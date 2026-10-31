import type { components } from "../api/types";
import PlayerLink from "./PlayerLink";

type Board = components["schemas"]["AvailabilityBoard"];

const REPORT_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  dateStyle: "medium",
  timeStyle: "short",
});

type Report = Pick<Board, "report_datetime" | "source" | "stale">;

/**
 * A report's source and time in Eastern, and the game date when given, marked stale when it is
 * ESPN's fallback; `none` when there is no report.
 */
export function ReportLine({
  report,
  gameDate,
  none,
}: {
  report: Report;
  gameDate?: string | null;
  none: string;
}) {
  return (
    <p className="report-line">
      {report.report_datetime === null ? (
        none
      ) : (
        <>
          {report.source === "espn" ? "ESPN" : "Official report"},{" "}
          {REPORT_TIME.format(new Date(report.report_datetime))} ET
          {gameDate && <> for {gameDate}</>}
        </>
      )}
      {report.stale && (
        <span className="marker marker-stale" title="ESPN's fallback, not the official report">
          stale
        </span>
      )}
    </p>
  );
}

/**
 * The team's newest injury report: each listed player's status and reason, and the rostered
 * players on G League assignment; marked stale when the report is ESPN's fallback (spec §4.3).
 */
function AvailabilityBoard({ board }: { board: Board }) {
  return (
    <>
      <ReportLine report={board} gameDate={board.game_date} none="No injury report yet." />
      {board.players.length === 0 ? (
        <p>Nobody listed.</p>
      ) : (
        <table className="availability-board">
          <thead>
            <tr>
              <th scope="col">Player</th>
              <th scope="col">Status</th>
              <th scope="col">Reason</th>
            </tr>
          </thead>
          <tbody>
            {board.players.map((row) => (
              <tr key={row.player.player_id}>
                <td>
                  <PlayerLink player={row.player} />
                  {row.g_league && <span className="badge">G League</span>}
                </td>
                <td>{row.status ?? "—"}</td>
                <td>{row.reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

export default AvailabilityBoard;
