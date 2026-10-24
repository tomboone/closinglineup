import { useParams } from "react-router";
import { useGame } from "../api/hooks";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

// A game is one date, so the page follows `?garbage_time=` but not `?date=` (spec §8).
function GamePage() {
  const gameId = useParams().gameId ?? "";
  const { garbageTime } = useShellParams();
  const game = useGame(gameId, { garbage_time: garbageTime });

  return (
    <>
      <h1>Game {gameId}</h1>
      <QueryStatus query={game}>
        {(page) => (
          <p>
            {page.away.tricode} at {page.home.tricode}, {page.game_date}.
          </p>
        )}
      </QueryStatus>
    </>
  );
}

export default GamePage;
