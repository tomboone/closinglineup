import { useParams } from "react-router";
import { usePlayer } from "../api/hooks";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

function PlayerPage() {
  const playerId = Number(useParams().id);
  const { date, garbageTime } = useShellParams();
  const player = usePlayer(playerId, { date, garbage_time: garbageTime });

  return (
    <>
      <h1>Player</h1>
      <QueryStatus query={player}>
        {(page) => (
          <p>
            {page.header.player.name ?? playerId}, as of {page.date}.
          </p>
        )}
      </QueryStatus>
    </>
  );
}

export default PlayerPage;
