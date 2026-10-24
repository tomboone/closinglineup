import type { MetaResponse } from "../api/client";

// The sources that always run, by label. ESPN's injuries are the fallback (spec §4.3), so having
// none means the official report has been enough; EPM, DARKO and contracts sit behind enable flags
// that `/api/meta` doesn't report (§4.5, §4.6), so having none may mean they are off.
const REQUIRED_SOURCES: Record<string, string> = {
  box_score: "box scores",
  injury_pdf: "official injury report",
  transactions: "transactions",
  bpm_local: "local BPM",
};

/** Warns when a team's injuries are ESPN's stale fallback, or a required source has given none. */
function StaleBanner({ meta }: { meta: MetaResponse }) {
  const espnTeams = meta.injury_sources.filter((marker) => marker.stale).map((m) => m.tricode);
  const missing = meta.freshness
    .filter(({ source, latest }) => latest === null && Object.hasOwn(REQUIRED_SOURCES, source))
    .map(({ source }) => REQUIRED_SOURCES[source]);

  if (espnTeams.length === 0 && missing.length === 0) return null;

  return (
    <aside className="stale-banner" aria-label="Stale data">
      {espnTeams.length > 0 && (
        <p>
          Injuries for {espnTeams.join(", ")} are from ESPN's fallback, not the official report.
        </p>
      )}
      {missing.length > 0 && <p>No data yet from: {missing.join(", ")}.</p>}
    </aside>
  );
}

export default StaleBanner;
