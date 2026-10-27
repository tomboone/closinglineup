import type { components } from "../api/types";
import SparklineCell, { type SparklineRange } from "./charts/SparklineCell";
import { TIER_COLOURS } from "./charts/theme";
import { decimal, percent, signed } from "./format";
import { BADGE_LABELS, IMPACT_LABELS, TIER_LABELS } from "./labels";
import PlayerLink from "./PlayerLink";

type Schemas = components["schemas"];
type Row = Schemas["TierBoardRow"];
type Tier = Schemas["Tier"];
type Point = Schemas["Point"];

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

const COLUMNS = 8;

/** Each sparkline column's y range over every row, so a column's lines read against one another. */
type Ranges = {
  minutesShare: SparklineRange | undefined;
  onOffNet: SparklineRange | undefined;
  impact: SparklineRange | undefined;
};

/**
 * The team's players grouped by tier in precedence order, each with his badges, three sparklines
 * over the team's last 20 game dates (minutes share, on/off net and impact) and his season Win
 * Shares and VORP. A column's sparklines share one y range, printed in its header.
 */
function TierBoard({ rows }: { rows: Row[] }) {
  const groups = TIER_ORDER.map((tier) => ({
    tier,
    rows: rows.filter((row) => row.tier === tier),
  })).filter((group) => group.rows.length > 0);

  if (groups.length === 0) return <p>No rotation state for this date.</p>;

  const ranges: Ranges = {
    minutesShare: columnRange(rows.map((row) => row.minutes_share)),
    onOffNet: columnRange(rows.map((row) => row.on_off_net), 0),
    impact: columnRange(rows.map((row) => row.impact), 0),
  };

  return (
    <table className="tier-board">
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Status</th>
          <th scope="col">Min share (10g)</th>
          <th scope="col">{withRange("Min share", ranges.minutesShare, percentRange)}</th>
          <th scope="col">{withRange("On/off net", ranges.onOffNet, signedRange)}</th>
          <th scope="col">{withRange(impactHeading(rows), ranges.impact, signedRange)}</th>
          <th scope="col">WS</th>
          <th scope="col">VORP</th>
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
            <TierBoardRow key={row.player.player_id} row={row} ranges={ranges} />
          ))}
        </tbody>
      ))}
    </table>
  );
}

function TierBoardRow({ row, ranges }: { row: Row; ranges: Ranges }) {
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
        <Sparkline series={row.minutes_share} range={ranges.minutesShare} format={percent} />
      </td>
      <td>
        <Sparkline series={row.on_off_net} baseline={0} range={ranges.onOffNet} format={signed} />
      </td>
      <td>
        <Sparkline series={row.impact} baseline={0} range={ranges.impact} format={signed} />
        {row.impact_source && <span className="impact-source">{IMPACT_LABELS[row.impact_source]}</span>}
      </td>
      <td className="number">{decimal(row.ws)}</td>
      <td className="number">{decimal(row.vorp)}</td>
    </tr>
  );
}

function Sparkline({
  series,
  baseline,
  range,
  format,
}: {
  series: Point[];
  baseline?: number;
  range: SparklineRange | undefined;
  format: (value: number) => string;
}) {
  return series.length > 0 ? (
    <SparklineCell series={series} baseline={baseline} range={range} endLabel format={format} />
  ) : (
    "—"
  );
}

/** The impact column's heading: the source's name when every row with one shares it. */
function impactHeading(rows: Row[]) {
  const sources = new Set(rows.flatMap((row) => row.impact_source ?? []));
  const [source] = sources;
  return sources.size === 1 ? IMPACT_LABELS[source] : "Impact";
}

function withRange(
  heading: string,
  range: SparklineRange | undefined,
  format: (range: SparklineRange) => string,
) {
  return range ? `${heading} (${format(range)})` : heading;
}

/**
 * The lowest and highest value over every series, taking in the baseline where there is one,
 * rounded outward to a readable step; none when no series has a value.
 */
function columnRange(series: Point[][], baseline?: number): SparklineRange | undefined {
  const values = series.flatMap((points) => points.flatMap((point) => point.value ?? []));
  if (values.length === 0) return undefined;
  if (baseline !== undefined) values.push(baseline);
  const min = Math.min(...values);
  const max = Math.max(...values);
  // About ten steps over the span, each 1, 2 or 5 times a power of ten; a column of one value
  // steps by its own size, or by a tenth when that value is zero.
  const span = max - min || Math.abs(max) || 1;
  const magnitude = 10 ** Math.floor(Math.log10(span / 10));
  const step = [1, 2, 5, 10].map((n) => n * magnitude).find((s) => s >= span / 10)!;
  // The epsilon keeps a value already on a step, but for float error, from moving a step out.
  const low = Math.floor(min / step + 1e-9) * step;
  const high = Math.ceil(max / step - 1e-9) * step;
  // A one-value column on a step is opened a step upward, so the range has a height to draw in.
  return { min: tidy(low), max: tidy(high > low ? high : low + step) };
}

/** A float rid of its representation error: 0.35000000000000003 → 0.35. */
function tidy(value: number) {
  return Number(value.toPrecision(12));
}

/** A range of fractions as whole-ish percentages: {0, 0.32} → "0–32%". */
function percentRange({ min, max }: SparklineRange) {
  return `${tidy(min * 100)}–${tidy(max * 100)}%`;
}

/** A range of ratings with their signs: {−15, 15} → "−15 to +15". */
function signedRange({ min, max }: SparklineRange) {
  const sign = (value: number) => (value > 0 ? `+${value}` : value < 0 ? `−${-value}` : "0");
  return `${sign(min)} to ${sign(max)}`;
}

export default TierBoard;
