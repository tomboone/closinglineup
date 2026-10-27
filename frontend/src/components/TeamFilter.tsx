import type { components } from "../api/types";

type TeamSummary = components["schemas"]["TeamSummary"];

/** A checkbox per tricode; none checked is every team. */
function TeamFilter({
  teams,
  selected,
  onChange,
}: {
  teams: TeamSummary[] | undefined;
  selected: ReadonlySet<string>;
  onChange: (teams: ReadonlySet<string>) => void;
}) {
  const toggle = (tricode: string, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(tricode);
    else next.delete(tricode);
    onChange(next);
  };
  return (
    <details className="team-filter">
      <summary>Teams: {selected.size === 0 ? "all" : [...selected].sort().join(", ")}</summary>
      <fieldset>
        <legend>Show teams</legend>
        {teams?.map((team) => (
          <label key={team.tricode}>
            <input
              type="checkbox"
              checked={selected.has(team.tricode)}
              onChange={(event) => toggle(team.tricode, event.target.checked)}
            />{" "}
            {team.tricode}
          </label>
        ))}
        <button type="button" disabled={selected.size === 0} onClick={() => onChange(new Set())}>
          All teams
        </button>
      </fieldset>
    </details>
  );
}

export default TeamFilter;
