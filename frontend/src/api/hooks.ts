import { QueryClient, useQuery } from "@tanstack/react-query";
import {
  ApiError,
  getGame,
  getLeague,
  getLeagueTable,
  getMeta,
  getPlayer,
  getSlate,
  getTeam,
  getTeams,
  type GameQuery,
  type LeagueQuery,
  type LeagueTableQuery,
  type PlayerQuery,
  type SlateQuery,
  type TeamQuery,
} from "./client";

// Pages fetch through these; charts fetch nothing themselves (spec §9). Each query's key carries
// its parameters, so a new `?date=` or `?garbage_time=` is a new query.

/** The data changes nightly, so a fetched page stays fresh for a while; a 4xx is not retried. */
export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5 * 60 * 1000,
        refetchOnWindowFocus: false,
        retry: (failures, error) =>
          !(error instanceof ApiError && error.status < 500) && failures < 2,
      },
    },
  });
}

export function useMeta() {
  return useQuery({ queryKey: ["meta"], queryFn: getMeta });
}

export function useTeams() {
  return useQuery({ queryKey: ["teams"], queryFn: getTeams });
}

export function useTeam(tricode: string, query: TeamQuery) {
  return useQuery({ queryKey: ["team", tricode, query], queryFn: () => getTeam(tricode, query) });
}

export function usePlayer(playerId: number, query: PlayerQuery) {
  return useQuery({
    queryKey: ["player", playerId, query],
    queryFn: () => getPlayer(playerId, query),
  });
}

export function useLeagueTable(query: LeagueTableQuery) {
  return useQuery({ queryKey: ["league-table", query], queryFn: () => getLeagueTable(query) });
}

/** `enabled` false holds the query until what it needs is known (the feed's last night). */
export function useLeague(query: LeagueQuery, { enabled = true } = {}) {
  return useQuery({ queryKey: ["league", query], queryFn: () => getLeague(query), enabled });
}

export function useSlate(query: SlateQuery) {
  return useQuery({ queryKey: ["slate", query], queryFn: () => getSlate(query) });
}

export function useGame(gameId: string, query: GameQuery) {
  return useQuery({ queryKey: ["game", gameId, query], queryFn: () => getGame(gameId, query) });
}
