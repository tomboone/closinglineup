import type { RouteObject } from "react-router";

// Placeholders until the real pages land (M6); each route renders only its name.
export const routes: RouteObject[] = [
  { path: "/", element: <h1>League</h1> },
  { path: "/teams/:tricode", element: <h1>Team</h1> },
  { path: "/players/:id", element: <h1>Player</h1> },
  { path: "/games/:gameId", element: <h1>Game</h1> },
];
