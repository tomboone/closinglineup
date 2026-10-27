import type { components, operations } from "./types";

// One typed fetcher per `/api` route the pages read (spec §8). The response and query types come
// from `types.ts`, which `pnpm gen:types` generates from the API's OpenAPI schema.

type Schemas = components["schemas"];

export type MetaResponse = Schemas["MetaResponse"];
export type TeamsResponse = Schemas["TeamsResponse"];
export type TeamPage = Schemas["TeamPage"];
export type PlayerPage = Schemas["PlayerPage"];
export type LeaguePage = Schemas["LeaguePage"];
export type GamePage = Schemas["GamePage"];
export type SlatePage = Schemas["SlatePage"];

type Query<Op extends keyof operations> = NonNullable<operations[Op]["parameters"]["query"]>;

export type TeamQuery = Query<"team_page_api_teams__tricode__get">;
export type PlayerQuery = Query<"player_page_api_players__player_id__get">;
export type LeagueQuery = Query<"league_api_league_get">;
export type GameQuery = Query<"game_page_api_games__game_id__get">;
export type SlateQuery = Query<"slate_api_slate_get">;
export type GarbageTime = NonNullable<GameQuery["garbage_time"]>;

/** A non-2xx response; `message` is the API's `detail` when it sent a string one. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The path with its query string, leaving out parameters that are unset. */
export function apiUrl(path: string, query: Record<string, string | number | null | undefined> = {}) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(name, String(value));
  }
  const search = params.toString();
  return search ? `${path}?${search}` : path;
}

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response));
  return (await response.json()) as T;
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      if (typeof body.detail === "string") return body.detail;
    }
  } catch {
    // Not JSON; fall through to the status line.
  }
  return `${response.status} ${response.statusText}`.trim();
}

export function getMeta() {
  return get<MetaResponse>("/api/meta");
}

export function getTeams() {
  return get<TeamsResponse>("/api/teams");
}

export function getTeam(tricode: string, query: TeamQuery = {}) {
  return get<TeamPage>(apiUrl(`/api/teams/${encodeURIComponent(tricode)}`, query));
}

export function getPlayer(playerId: number, query: PlayerQuery = {}) {
  return get<PlayerPage>(apiUrl(`/api/players/${playerId}`, query));
}

export function getLeague(query: LeagueQuery = {}) {
  return get<LeaguePage>(apiUrl("/api/league", query));
}

export function getGame(gameId: string, query: GameQuery = {}) {
  return get<GamePage>(apiUrl(`/api/games/${encodeURIComponent(gameId)}`, query));
}

export function getSlate(query: SlateQuery) {
  return get<SlatePage>(apiUrl("/api/slate", query));
}
