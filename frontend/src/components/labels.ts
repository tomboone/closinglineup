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

export const IMPACT_LABELS: Record<Schemas["ImpactSeries"]["source"], string> = {
  epm: "EPM",
  darko: "DARKO",
  bpm_local: "BPM",
};
