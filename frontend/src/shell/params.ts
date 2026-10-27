import { useSearchParams } from "react-router";
import type { GarbageTime } from "../api/client";

/** The feed's tabs, in the order the tab list shows them (v2 spec §4). */
export const TABS = ["today", "rotation", "movers", "flags", "moves"] as const;
export type Tab = (typeof TABS)[number];

const isTab = (value: string | null): value is Tab => TABS.some((tab) => tab === value);

/**
 * The shell's global parameters, kept in the URL so every page and every link shares them:
 * `?date=` (unset means the page's default: the feed's is today, the others' the API's, the latest
 * derived date), `?garbage_time=include` (unset means the API's default, exclude), and the feed's
 * `?tab=` (unset means `today`) and `?team=PHI,LAL` (unset means every team) (v2 spec §4).
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
    tab: isTab(tab) ? tab : "today",
    teams,
    setDate: (date: string | undefined) => set("date", date || undefined),
    setGarbageTime: (value: GarbageTime) =>
      set("garbage_time", value === "include" ? value : undefined),
    setTab: (value: Tab) => set("tab", value === "today" ? undefined : value),
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
