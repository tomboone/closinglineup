import type { components } from "../api/types";
import SparklineCell from "./charts/SparklineCell";
import { TIER_COLOURS } from "./charts/theme";
import { percent } from "./format";
import { BADGE_LABELS, IMPACT_LABELS, TIER_LABELS } from "./labels";
import PlayerLink from "./PlayerLink";

type Schemas = components["schemas"];
type Row = Schemas["TierBoardRow"];
type Tier = Schemas["Tier"];

/** The tiers in the order they are evaluated, the first match winning (spec §7.5). */
const TIER_ORDER: Tier[] = ["injured", "starter", "core", "fringe", "out"];

// `active` is the usual case, and `g_league` already shows as a badge, so neither shows here.
const AVAILABILITY_LABELS: Record<Schemas["Availability"], string> = {
  active: "",
  out: "Out",
  doubtful: "Doubtful",
  questionable: "Questionable",
  probable: "Probable",
  available: "Available",
  inactive: "Inactive",
  suspended: "Suspended",
  g_league: "",
};

const COLUMNS = 6;

/**
 * The team's players grouped by tier in precedence order, each with his badges and three
 * sparklines over the team's last 20 game dates: minutes share, on/off net and impact.
 */
function TierBoard({ rows }: { rows: Row[] }) {
  const groups = TIER_ORDER.map((tier) => ({
    tier,
    rows: rows.filter((row) => row.tier === tier),
  })).filter((group) => group.rows.length > 0);

  if (groups.length === 0) return <p>No rotation state for this date.</p>;

  return (
    <table className="tier-board">
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Status</th>
          <th scope="col">Min share (10g)</th>
          <th scope="col">Minutes share</th>
          <th scope="col">On/off net</th>
          <th scope="col">Impact</th>
        </tr>
      </thead>
      {groups.map((group) => (
        <tbody key={group.tier} aria-labelledby={`tier-${group.tier}`}>
          <tr className="tier-heading">
            <th
              id={`tier-${group.tier}`}
              colSpan={COLUMNS}
              scope="rowgroup"
              style={{ borderLeftColor: TIER_COLOURS[group.tier] }}
            >
              {`${TIER_LABELS[group.tier]} (${group.rows.length})`}
            </th>
          </tr>
          {group.rows.map((row) => (
            <TierBoardRow key={row.player.player_id} row={row} />
          ))}
        </tbody>
      ))}
    </table>
  );
}

function TierBoardRow({ row }: { row: Row }) {
  return (
    <tr>
      <td>
        <PlayerLink player={row.player} />
        {row.badges.map((badge) => (
          <span key={badge} className="badge">
            {BADGE_LABELS[badge]}
          </span>
        ))}
        {row.is_short && (
          <span className="marker" title="Role window shorter than its nominal length">
            short window
          </span>
        )}
      </td>
      <td>{AVAILABILITY_LABELS[row.availability]}</td>
      <td className="number">{percent(row.minutes_share_10)}</td>
      <td>
        <Sparkline series={row.minutes_share} />
      </td>
      <td>
        <Sparkline series={row.on_off_net} baseline={0} />
      </td>
      <td>
        <Sparkline series={row.impact} baseline={0} />
        {row.impact_source && <span className="impact-source">{IMPACT_LABELS[row.impact_source]}</span>}
      </td>
    </tr>
  );
}

function Sparkline({ series, baseline }: { series: Schemas["Point"][]; baseline?: number }) {
  return series.length > 0 ? <SparklineCell series={series} baseline={baseline} /> : "—";
}

export default TierBoard;
