import { Link, Outlet, useLocation, useMatch, useNavigate } from "react-router";
import { useMeta, useTeams } from "../api/hooks";
import { useShellParams } from "./params";
import StaleBanner from "./StaleBanner";

/** The app shell: a header with the team switcher and the global controls, above every page. */
function Layout() {
  const meta = useMeta();
  const { search } = useLocation();
  const { date, garbageTime, setDate, setGarbageTime } = useShellParams();
  const latest = meta.data?.latest_derived_date ?? undefined;

  return (
    <div className="shell">
      <header className="shell-header">
        <Link className="brand" to={{ pathname: "/", search }}>
          closinglineup
        </Link>
        <TeamSwitcher />
        <label>
          Date{" "}
          <input
            type="date"
            value={date ?? latest ?? ""}
            max={latest}
            onChange={(event) => setDate(event.target.value)}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={garbageTime === "include"}
            onChange={(event) => setGarbageTime(event.target.checked ? "include" : "exclude")}
          />{" "}
          Include garbage time
        </label>
      </header>
      {meta.data && <StaleBanner meta={meta.data} />}
      <main>
        <Outlet />
      </main>
    </div>
  );
}

/** Goes to a team's page, keeping the global parameters. */
function TeamSwitcher() {
  const teams = useTeams();
  const navigate = useNavigate();
  const { search } = useLocation();
  const current = useMatch("/teams/:tricode")?.params.tricode ?? "";

  return (
    <select
      aria-label="Team"
      value={current}
      disabled={!teams.data}
      onChange={(event) => navigate({ pathname: `/teams/${event.target.value}`, search })}
    >
      <option value="" disabled>
        {teams.isError ? "Teams unavailable" : "Choose a team"}
      </option>
      {teams.data?.teams.map((team) => (
        <option key={team.tricode} value={team.tricode}>
          {team.tricode} — {team.name}
        </option>
      ))}
    </select>
  );
}

export default Layout;
