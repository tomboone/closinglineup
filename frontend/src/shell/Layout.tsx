import { Link, Outlet, useLocation, useMatch, useNavigate } from "react-router";
import { useMeta, useTeams } from "../api/hooks";
import { easternToday, useShellParams } from "./params";
import StaleBanner from "./StaleBanner";

/**
 * The app shell: a header with the team switcher, the league table's link and the global controls,
 * above every page. The date picker reaches today: on the feed its date is a morning, today when
 * unset; elsewhere it is a game date, the latest derived when unset (v2 spec §3).
 */
function Layout() {
  const meta = useMeta();
  const { search } = useLocation();
  const feed = useMatch("/") !== null;
  const { date, garbageTime, setDate, setGarbageTime } = useShellParams();
  const today = easternToday();
  const latest = meta.data?.latest_derived_date ?? undefined;

  return (
    <div className="shell">
      <header className="shell-header">
        <Link className="brand" to={{ pathname: "/", search }}>
          closinglineup
        </Link>
        <TeamSwitcher />
        <Link to={{ pathname: "/players", search }}>Players</Link>
        <label>
          Date{" "}
          <input
            type="date"
            value={date ?? (feed ? today : latest) ?? ""}
            max={today}
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
