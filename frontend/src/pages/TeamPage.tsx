import { useParams } from "react-router";
import { useTeam } from "../api/hooks";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

function TeamPage() {
  const tricode = useParams().tricode ?? "";
  const { date, garbageTime } = useShellParams();
  const team = useTeam(tricode, { date, garbage_time: garbageTime });

  return (
    <>
      <h1>Team {tricode}</h1>
      <QueryStatus query={team}>
        {(page) => (
          <p>
            {page.name}, as of {page.tier_date ?? page.date}.
          </p>
        )}
      </QueryStatus>
    </>
  );
}

export default TeamPage;
