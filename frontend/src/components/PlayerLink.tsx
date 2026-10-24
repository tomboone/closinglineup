import { Link, useLocation } from "react-router";
import type { components } from "../api/types";

type PlayerRef = components["schemas"]["PlayerRef"];

/** A player's name linking to his page, keeping the global `?date=` and `?garbage_time=`. */
function PlayerLink({ player }: { player: PlayerRef }) {
  const { search } = useLocation();
  return (
    <Link to={{ pathname: `/players/${player.player_id}`, search }}>
      {player.name ?? `Player ${player.player_id}`}
    </Link>
  );
}

/** A lineup's players, comma-separated, each linking to his page. */
export function PlayerList({ players }: { players: PlayerRef[] }) {
  return players.map((player, i) => (
    <span key={player.player_id}>
      {i > 0 && ", "}
      <PlayerLink player={player} />
    </span>
  ));
}

export default PlayerLink;
