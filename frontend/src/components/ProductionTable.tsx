import type { components } from "../api/types";
import { percent, signed } from "./format";

type Schemas = components["schemas"];
type Row = Schemas["ProductionRow"];
type Cell = Schemas["ProductionCell"];
type Format = Row["format"];

const COLUMNS: { window: keyof Row["windows"]; label: string }[] = [
  { window: 5, label: "Last 5" },
  { window: 10, label: "Last 10" },
  { window: 20, label: "Last 20" },
  { window: "season", label: "Season" },
  { window: "prior_season", label: "Prior season" },
];

const DASH = "—";

const decimal = (value: number | null) => (value == null ? DASH : value.toFixed(1));

/** A ratio to three decimals, as Basketball Reference prints FTr and 3PAr: 0.345 → ".345". */
const ratio = (value: number) => value.toFixed(3).replace(/^0\./, ".");

function formatValue(format: Format, value: number | null) {
  if (format === "percent") return percent(value);
  if (format === "rate") return value == null ? DASH : ratio(value);
  return decimal(value);
}

/** A difference in the metric's units, a percentage's in points: 0.012 → "+1.2". */
function formatDelta(format: Format, delta: number) {
  if (format === "percent") return signed(delta * 100);
  if (format === "rate") {
    const fixed = ratio(Math.abs(delta));
    if (Number(fixed) === 0) return fixed;
    return delta > 0 ? `+${fixed}` : `−${fixed}`;
  }
  return signed(delta);
}

/**
 * His production metrics over each window, a row per metric: each cell the window's value with its
 * difference from the season's in small type, "—" where he has no such window.
 */
function ProductionTable({ rows }: { rows: Row[] }) {
  if (rows.every((row) => Object.values(row.windows).every((cell) => cell === null))) {
    return <p>No games yet.</p>;
  }
  return (
    <table className="production">
      <thead>
        <tr>
          <th scope="col">Metric</th>
          {COLUMNS.map((column) => (
            <th key={column.window} scope="col">
              {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.metric}>
            <th scope="row">{row.label}</th>
            {COLUMNS.map(({ window }) => (
              <ProductionCell
                key={window}
                row={row}
                cell={row.windows[window]}
                // The season's difference from itself says nothing.
                showDelta={window !== "season"}
              />
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ProductionCell({ row, cell, showDelta }: { row: Row; cell: Cell | null; showDelta: boolean }) {
  if (cell === null) return <td className="number">{DASH}</td>;
  const title =
    row.metric === "ws" ? `OWS ${decimal(cell.o_value)} · DWS ${decimal(cell.d_value)}` : undefined;
  return (
    <td className="number" title={title}>
      {formatValue(row.format, cell.value)}
      {showDelta && cell.delta_season !== null && (
        <span className="delta">{formatDelta(row.format, cell.delta_season)}</span>
      )}
      {cell.is_short && (
        <span className="marker" title={`Window shorter than its nominal length: ${cell.games} games`}>
          short window
        </span>
      )}
    </td>
  );
}

export default ProductionTable;
