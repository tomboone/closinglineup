// Number formats shared by the page tables. A missing value reads as an em dash.

const DASH = "—";

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
