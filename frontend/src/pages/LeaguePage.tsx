import { useLeague } from "../api/hooks";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

// The league morning page reads the date alone (spec §8). Its content is NEU-1888.
function LeaguePage() {
  const { date } = useShellParams();
  const league = useLeague({ date });

  return (
    <>
      <h1>League</h1>
      <QueryStatus query={league}>{(page) => <p>Morning feed for {page.date}.</p>}</QueryStatus>
    </>
  );
}

export default LeaguePage;
