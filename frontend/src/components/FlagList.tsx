import type { components } from "../api/types";
import { money, percent, signed } from "./format";

type Schemas = components["schemas"];
type RaisedFlag = Schemas["RaisedFlag"];
type Detail = RaisedFlag["detail"];

const FLAG_LABELS: Record<Schemas["Flag"], string> = {
  minutes_cliff: "Minutes cliff",
  usage_up_efficiency_down: "Usage up, efficiency down",
  bleeding: "Bleeding",
  earning_trust: "Earning trust",
  yo_yo: "Yo-yo",
  contract_pressure: "Contract pressure",
};

const num = (value: unknown) => (typeof value === "number" ? value : null);

/** The numbers that raised a flag, as `derive/flags.py` keeps them in its `detail`. */
function describe(flag: Schemas["Flag"], detail: Detail): string {
  switch (flag) {
    case "minutes_cliff":
      return `5-game share ${percent(num(detail.share_5))} vs 20-game ${percent(num(detail.share_20))}`;
    case "usage_up_efficiency_down":
      return (
        `USG% ${percent(num(detail.usg_pct_10))} (${signed(scaled(detail.usg_pct_delta_season))} pts), ` +
        `TS% ${percent(num(detail.ts_pct_10))} (${signed(scaled(detail.ts_pct_delta_season))} pts) vs season`
      );
    case "bleeding":
      return (
        `On-court net ${signed(num(detail.on_court_net))} on ${num(detail.possessions) ?? "—"} poss, ` +
        `off-court ${signed(num(detail.off_court_net))}`
      );
    case "earning_trust": {
      const shares = Array.isArray(detail.shares) ? detail.shares : [];
      return `Minutes share ${shares.map((share) => percent(num(share))).join(" → ")}`;
    }
    case "yo_yo":
      return `${num(detail.events) ?? "—"} rotation events in the last 20 games`;
    case "contract_pressure":
      return detail.reason === "two_way"
        ? `Two-way: ${num(detail.active_games) ?? "—"} of ${num(detail.limit) ?? "—"} active games`
        : `${money(num(detail.guaranteed_after))} guaranteed on ${String(detail.guarantee_date)}, ` +
            `in ${num(detail.days) ?? "—"} days`;
  }
}

// The deltas are fractions; their flags read in percentage points.
function scaled(value: unknown) {
  const n = num(value);
  return n === null ? null : n * 100;
}

/** Raised flags, as given (the API sends them newest first), each with what raised it. */
function FlagList({ flags }: { flags: RaisedFlag[] }) {
  if (flags.length === 0) return <p>No flags raised.</p>;
  return (
    <ul className="flag-list">
      {flags.map((raised) => (
        <li key={`${raised.game_date}-${raised.flag}`}>
          <span className="flag-date">{raised.game_date}</span>{" "}
          <strong>{FLAG_LABELS[raised.flag]}</strong>{" "}
          <span className="note">{describe(raised.flag, raised.detail)}</span>
        </li>
      ))}
    </ul>
  );
}

export default FlagList;
