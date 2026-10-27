import type { RouteObject } from "react-router";
import GamePage from "./pages/GamePage";
import LeaguePage from "./pages/LeaguePage";
import PlayerPage from "./pages/PlayerPage";
import PlayersPage from "./pages/PlayersPage";
import TeamPage from "./pages/TeamPage";
import Layout from "./shell/Layout";

// Every page renders inside the shell, which owns the global `?date=` and `?garbage_time=`.
export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { path: "/", element: <LeaguePage /> },
      { path: "/teams/:tricode", element: <TeamPage /> },
      { path: "/players", element: <PlayersPage /> },
      { path: "/players/:id", element: <PlayerPage /> },
      { path: "/games/:gameId", element: <GamePage /> },
    ],
  },
  // A page of every chart with sample data, outside the shell so it needs no API. `DEV` is false in
  // a build, which drops this branch and with it the lazy import, so the bundle has no such route.
  ...(import.meta.env.DEV
    ? [
        {
          path: "/dev/charts",
          lazy: async () => ({ Component: (await import("./dev/ChartsPage")).default }),
          hydrateFallbackElement: <p role="status">Loading…</p>,
        },
      ]
    : []),
];
