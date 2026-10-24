import type { MetaResponse } from "../api/client";

const SOURCE_LABELS: Record<string, string> = {
  box_score: "box scores",
  injury_pdf: "official injury report",
  transactions: "transactions",
  epm: "EPM",
  darko: "DARKO",
  bpm_local: "local BPM",
  contracts: "contracts",
};

// ESPN's injuries are the fallback (spec §4.3): having none of them means the official report has
// been enough, not that a source is missing.
const FALLBACK_SOURCES = new Set(["injury_espn"]);

/** Warns when a team's injuries come from ESPN's stale fallback, or a source has given nothing. */
function StaleBanner({ meta }: { meta: MetaResponse }) {
  const espnTeams = meta.injury_sources.filter((marker) => marker.stale).map((m) => m.tricode);
  const missing = meta.freshness
    .filter(({ source, latest }) => latest === null && !FALLBACK_SOURCES.has(source))
    .map(({ source }) => SOURCE_LABELS[source] ?? source);

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
