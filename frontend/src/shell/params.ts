import { useSearchParams } from "react-router";
import type { GarbageTime } from "../api/client";

/** The feed's tabs, in the order the tab list shows them (v2 spec §4). */
export const TABS = ["today", "rotation", "movers", "flags", "moves"] as const;
export type Tab = (typeof TABS)[number];

/** The team page's tabs, in the order its tab list shows them (NEU-2001). */
export const TEAM_TABS = ["rotation", "availability", "lineups", "contracts"] as const;
export type TeamTab = (typeof TEAM_TABS)[number];

const isOneOf = <T extends string>(values: readonly T[], value: string | null): value is T =>
  values.some((one) => one === value);

/**
 * The shell's global parameters, kept in the URL so every page and every link shares them:
 * `?date=` (unset means the page's default: the feed's is today, the others' the API's, the latest
 * derived date), `?garbage_time=include` (unset means the API's default, exclude), the feed's
 * `?tab=` (unset means `today`) and `?team=PHI,LAL` (unset means every team) (v2 spec §4), and the
 * team page's `?tab=` (unset means `rotation`). A tab one page doesn't have reads as its default.
 */
export function useShellParams() {
  const [searchParams, setSearchParams] = useSearchParams();

  const set = (name: string, value: string | undefined) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (value === undefined) next.delete(name);
      else next.set(name, value);
      return next;
    });
  };

  const garbageTime: GarbageTime =
    searchParams.get("garbage_time") === "include" ? "include" : "exclude";
  const tab = searchParams.get("tab");
  const teams = new Set((searchParams.get("team") ?? "").split(",").filter(Boolean));

  return {
    date: searchParams.get("date") || undefined,
    garbageTime,
    tab: isOneOf(TABS, tab) ? tab : "today",
    teamTab: isOneOf(TEAM_TABS, tab) ? tab : "rotation",
    teams,
    setDate: (date: string | undefined) => set("date", date || undefined),
    setGarbageTime: (value: GarbageTime) =>
      set("garbage_time", value === "include" ? value : undefined),
    setTab: (value: Tab) => set("tab", value === "today" ? undefined : value),
    setTeamTab: (value: TeamTab) => set("tab", value === "rotation" ? undefined : value),
    setTeams: (value: ReadonlySet<string>) => set("team", [...value].join(",") || undefined),
  };
}

/** Today in Eastern time, the feed's default morning (v2 spec §3), as `YYYY-MM-DD`. */
export function easternToday(now: Date = new Date()) {
  // en-CA formats a date as YYYY-MM-DD.
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(now);
}

/** The calendar date before a `YYYY-MM-DD` one. */
export function dayBefore(date: string) {
  const day = new Date(`${date}T00:00:00Z`);
  day.setUTCDate(day.getUTCDate() - 1);
  return day.toISOString().slice(0, 10);
}
