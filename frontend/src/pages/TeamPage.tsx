import { Link, useLocation, useParams } from "react-router";
import type { TeamPage as TeamPageData } from "../api/client";
import { useTeam } from "../api/hooks";
import AvailabilityBoard from "../components/AvailabilityBoard";
import { money } from "../components/format";
import LineupTable from "../components/LineupTable";
import PlayerLink, { PlayerList } from "../components/PlayerLink";
import TierBoard from "../components/TierBoard";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

/** The daily-driver view of one team: its rotation, lineups, availability and contracts. */
function TeamPage() {
  const tricode = useParams().tricode ?? "";
  const { date, garbageTime } = useShellParams();
  const team = useTeam(tricode, { date, garbage_time: garbageTime });

  return (
    <>
      <h1>Team {tricode}</h1>
      <QueryStatus query={team}>{(page) => <TeamPageBody page={page} />}</QueryStatus>
    </>
  );
}

function TeamPageBody({ page }: { page: TeamPageData }) {
  const { roster } = page;
  const garbage = page.garbage_time === "include" ? "included" : "excluded";

  return (
    <>
      <p>
        {page.name}, as of {page.tier_date ?? page.date}.
      </p>
      <p className="roster-counts">
        Roster: {roster.standard} of {roster.standard_limit} standard, {roster.two_way} of{" "}
        {roster.two_way_limit} two-way.
      </p>
      <div className="team-page">
        <section className="team-tiers" aria-labelledby="team-tiers">
          <h2 id="team-tiers">Rotation</h2>
          <p className="note">
            Minutes share and on/off net are 10-game windows over the last 20 games, garbage time
            always excluded.
          </p>
          <TierBoard rows={page.tier_board} />
        </section>
        <section aria-labelledby="team-availability">
          <h2 id="team-availability">Availability</h2>
          <AvailabilityBoard board={page.availability} />
        </section>
        <section aria-labelledby="team-closing">
          <h2 id="team-closing">Closing lineups</h2>
          <ClosingLineups lineups={page.closing_lineups} />
        </section>
        <section aria-labelledby="team-lineups">
          <h2 id="team-lineups">Top lineups</h2>
          <p className="note">By possessions this season, garbage time {garbage}.</p>
          <LineupTable caption="5-man lineups" rows={page.lineups} />
          <LineupTable caption="2-man combos" rows={page.combos} />
        </section>
        <section aria-labelledby="team-contracts">
          <h2 id="team-contracts">Contract notes</h2>
          <ContractNotes contracts={page.contracts} />
        </section>
      </div>
    </>
  );
}

/** The fives on the floor at the end of the team's last close games, each linking to its game. */
function ClosingLineups({ lineups }: { lineups: TeamPageData["closing_lineups"] }) {
  const { search } = useLocation();
  if (lineups.length === 0) return <p>No close games yet.</p>;
  return (
    <ul className="closing-lineups">
      {lineups.map((lineup) => (
        <li key={lineup.game_id}>
          <Link to={{ pathname: `/games/${lineup.game_id}`, search }}>
            {lineup.game_date} vs {lineup.opponent}
          </Link>{" "}
          <span className={lineup.margin > 0 ? "result-win" : "result-loss"}>
            {lineup.margin > 0 ? `W +${lineup.margin}` : `L −${-lineup.margin}`}
          </span>
          <div>
            <PlayerList players={lineup.players} />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** The rostered players with non-guaranteed money, by guarantee date. */
function ContractNotes({ contracts }: { contracts: TeamPageData["contracts"] }) {
  if (contracts.length === 0) return <p>No non-guaranteed contracts on file.</p>;
  return (
    <table className="contract-notes">
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Salary</th>
          <th scope="col">Guaranteed</th>
          <th scope="col">Guarantee date</th>
          <th scope="col">Guaranteed after</th>
        </tr>
      </thead>
      <tbody>
        {contracts.map((note) => (
          <tr key={`${note.player.player_id}-${note.guarantee_date}`}>
            <td>
              <PlayerLink player={note.player} />
              {note.minimum_salary && <span className="badge">Minimum</span>}
            </td>
            <td className="number">{money(note.salary)}</td>
            <td className="number">{money(note.guaranteed)}</td>
            <td>
              {note.guarantee_date} (
              {note.days_until_guarantee === 0 ? "today" : `in ${note.days_until_guarantee} days`})
            </td>
            <td className="number">{money(note.guaranteed_after)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default TeamPage;
