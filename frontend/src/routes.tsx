import type { RouteObject } from "react-router";
import GamePage from "./pages/GamePage";
import LeaguePage from "./pages/LeaguePage";
import PlayerPage from "./pages/PlayerPage";
import TeamPage from "./pages/TeamPage";
import Layout from "./shell/Layout";

// Every page renders inside the shell, which owns the global `?date=` and `?garbage_time=`.
export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { path: "/", element: <LeaguePage /> },
      { path: "/teams/:tricode", element: <TeamPage /> },
      { path: "/players/:id", element: <PlayerPage /> },
      { path: "/games/:gameId", element: <GamePage /> },
    ],
  },
];
