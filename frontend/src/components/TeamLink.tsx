import { Link, useLocation } from "react-router";

/** A team's tricode linking to its page, keeping the global `?date=` and `?garbage_time=`. */
function TeamLink({ tricode }: { tricode: string }) {
  const { search } = useLocation();
  return <Link to={{ pathname: `/teams/${tricode}`, search }}>{tricode}</Link>;
}

export default TeamLink;
