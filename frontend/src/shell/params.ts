import { useSearchParams } from "react-router";
import type { GarbageTime } from "../api/client";

/**
 * The shell's global parameters, kept in the URL so every page and every link shares them:
 * `?date=` (unset means the API's default, the latest derived date) and `?garbage_time=include`
 * (unset means the API's default, exclude).
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

  return {
    date: searchParams.get("date") || undefined,
    garbageTime,
    setDate: (date: string | undefined) => set("date", date || undefined),
    setGarbageTime: (value: GarbageTime) =>
      set("garbage_time", value === "include" ? value : undefined),
  };
}
