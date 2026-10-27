// Number formats shared by the page tables. A missing value reads as an em dash.

export const DASH = "—";

/** A fraction as a percentage to one decimal: 0.123 → "12.3%". */
export function percent(value: number | null | undefined) {
  return value == null ? DASH : `${(value * 100).toFixed(1)}%`;
}

/** A rating with its sign to one decimal: 4.25 → "+4.3", -3 → "−3.0". */
export function signed(value: number | null | undefined) {
  if (value == null) return DASH;
  const fixed = Math.abs(value).toFixed(1);
  if (Number(fixed) === 0) return fixed;
  return value > 0 ? `+${fixed}` : `−${fixed}`;
}

/** A number to one decimal: 12.34 → "12.3". */
export function decimal(value: number | null | undefined) {
  return value == null ? DASH : value.toFixed(1);
}

/** A ratio to three decimals, as Basketball Reference prints FTr and 3PAr: 0.345 → ".345". */
export function ratio(value: number | null | undefined) {
  return value == null ? DASH : value.toFixed(3).replace(/^0\./, ".");
}

/** A difference of ratios with its sign: 0.012 → "+.012", -0.04 → "−.040". */
export function signedRatio(value: number | null | undefined) {
  if (value == null) return DASH;
  const fixed = ratio(Math.abs(value));
  if (Number(fixed) === 0) return fixed;
  return value > 0 ? `+${fixed}` : `−${fixed}`;
}

/** Seconds played as the box score shows them: 2052 → "34:12". */
export function minutes(seconds: number | null | undefined) {
  if (seconds == null) return DASH;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

const DOLLARS = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

/** Whole dollars: 1234567 → "$1,234,567". */
export function money(value: number | null | undefined) {
  return value == null ? DASH : DOLLARS.format(value);
}
