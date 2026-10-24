import type { components } from "../api/types";

// Display names for the API's enums, shared by the pages so a tier or a badge reads the same everywhere.

type Schemas = components["schemas"];

export const TIER_LABELS: Record<Schemas["Tier"], string> = {
  injured: "Injured",
  starter: "Starter",
  core: "Core",
  fringe: "Fringe",
  out: "Out",
};

export const BADGE_LABELS: Record<Schemas["Badge"], string> = {
  two_way: "Two-way",
  ten_day: "10-day",
  exhibit_10: "Exhibit 10",
  g_league: "G League",
};

// The API sends a transaction's type as a string; these are the types `sources/transactions.py`
// writes (spec §4.4), any other reading as itself.
export const TRANSACTION_LABELS: Record<string, string> = {
  sign: "Signed",
  waive: "Waived",
  trade: "Traded",
  two_way_sign: "Two-way signing",
  two_way_convert: "Two-way converted",
  assign: "G League assignment",
  recall: "G League recall",
  option_exercise: "Option exercised",
  option_decline: "Option declined",
  other: "Other",
};

export const IMPACT_LABELS: Record<Schemas["ImpactSeries"]["source"], string> = {
  epm: "EPM",
  darko: "DARKO",
  bpm_local: "BPM",
};
