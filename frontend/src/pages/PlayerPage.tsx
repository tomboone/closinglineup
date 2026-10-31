import { useMemo } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router";
import type { PlayerPage as PlayerPageData } from "../api/client";
import { usePlayer } from "../api/hooks";
import type { components } from "../api/types";
import AnnotatedTimeSeries from "../components/charts/AnnotatedTimeSeries";
import type { NamedSeries } from "../components/charts/AnnotatedTimeSeries";
import ShotProfile from "../components/charts/ShotProfile";
import type { ShotWindow } from "../components/charts/ShotProfile";
import { TIER_COLOURS } from "../components/charts/theme";
import TierStrip from "../components/charts/TierStrip";
import FlagList from "../components/FlagList";
import { money, percent, signed } from "../components/format";
import PlayerLink from "../components/PlayerLink";
import ProductionTable from "../components/ProductionTable";
import { AVAILABILITY_LABELS, BADGE_LABELS, IMPACT_LABELS, TIER_LABELS } from "../components/labels";
import { useShellParams } from "../shell/params";
import QueryStatus from "./QueryStatus";

type Schemas = components["schemas"];
type Window = Schemas["Window"];

// The API's default window, which the URL leaves unset.
const DEFAULT_WINDOW: Window = "10";

const WINDOWS: { window: Window; label: string }[] = [
  { window: "5", label: "Last 5" },
  { window: "10", label: "Last 10" },
  { window: "20", label: "Last 20" },
  { window: "season", label: "Season" },
];

const WINDOW_LABELS = Object.fromEntries(WINDOWS.map((w) => [w.window, w.label])) as Record<
  Window,
  string
>;

const oneDecimal = (value: number) => value.toFixed(1);

/** The page's `?window=`, kept in the URL beside the shell's parameters so links carry it. */
function useWindowParam() {
  const [searchParams, setSearchParams] = useSearchParams();
  const param = searchParams.get("window");
  const window = WINDOWS.find((w) => w.window === param)?.window ?? DEFAULT_WINDOW;

  const setWindow = (value: Window) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (value === DEFAULT_WINDOW) next.delete("window");
      else next.set("window", value);
      return next;
    });
  };

  return { window, setWindow };
}

/** One player's trends over a window of his team games, with what happened along the way. */
function PlayerPage() {
  const playerId = Number(useParams().id);
  const { date, garbageTime } = useShellParams();
  const { window, setWindow } = useWindowParam();
  const player = usePlayer(playerId, {
    date,
    garbage_time: garbageTime,
    window: window === DEFAULT_WINDOW ? undefined : window,
  });

  // The heading and the toggle stay put while a new window loads.
  return (
    <>
      <h1>{player.data?.header.player.name ?? "Player"}</h1>
      <WindowToggle window={window} onChange={setWindow} />
      <QueryStatus query={player}>
        {(page) => (
          <>
            <PlayerHeader header={page.header} date={page.date} />
            {page.contract && <ContractLine contract={page.contract} />}
            <PlayerPageBody page={page} />
          </>
        )}
      </QueryStatus>
    </>
  );
}

/** His team, position and tier, his availability beside it, and his line on the newest injury report. */
function PlayerHeader({ header, date }: { header: PlayerPageData["header"]; date: string }) {
  const { search } = useLocation();
  const availability = header.availability && AVAILABILITY_LABELS[header.availability];
  const { injury } = header;
  return (
    <header className="player-header">
      <p>
        {header.tricode ? (
          <Link to={{ pathname: `/teams/${header.tricode}`, search }}>{header.tricode}</Link>
        ) : (
          "No team"
        )}
        {header.position && <>, {header.position}</>}
        {header.tier && (
          <span className="tier" style={{ borderLeftColor: TIER_COLOURS[header.tier] }}>
            {TIER_LABELS[header.tier]}
          </span>
        )}
        {availability && <span className="availability">{availability}</span>}
        {header.badges.map((badge) => (
          <span key={badge} className="badge">
            {BADGE_LABELS[badge]}
          </span>
        ))}
        <span className="note"> as of {date}</span>
      </p>
      {injury && (
        <p className="injury-line">
          Injury report: {injury.status}
          {injury.reason && <> ({injury.reason})</>}
        </p>
      )}
    </header>
  );
}

/** His non-guaranteed contract, as the team page's contract notes show it; an amount no source gives is left out. */
function ContractLine({ contract }: { contract: Schemas["ContractNote"] }) {
  const days = contract.days_until_guarantee;
  const when = `${contract.guarantee_date} (${days === 0 ? "today" : `in ${days} days`})`;
  return (
    <p className="contract-line">
      Non-guaranteed{contract.minimum_salary && " minimum"} contract: {money(contract.guaranteed)}
      {contract.salary !== null && <> of {money(contract.salary)}</>} guaranteed,{" "}
      {contract.guaranteed_after === null ? `rising ${when}` : `${money(contract.guaranteed_after)} from ${when}`}.
    </p>
  );
}

function WindowToggle({ window, onChange }: { window: Window; onChange: (window: Window) => void }) {
  return (
    <fieldset className="window-toggle">
      <legend>Window</legend>
      {WINDOWS.map((w) => (
        <label key={w.window}>
          <input
            type="radio"
            name="window"
            value={w.window}
            checked={w.window === window}
            onChange={() => onChange(w.window)}
          />
          {w.label}
        </label>
      ))}
    </fieldset>
  );
}

function PlayerPageBody({ page }: { page: PlayerPageData }) {
  const { series, annotations } = page;
  const garbage = page.garbage_time === "include" ? "included" : "excluded";

  // Built once per page, so the charts set their options once per fetch.
  const charts = useMemo(
    () => ({
      // The share as a percentage, so it reads on the minutes' scale: a 36-minute game is 15%.
      minutes: [
        { name: "Minutes", points: series.minutes },
        {
          name: "Share of team minutes (%)",
          points: series.minutes_share.map((point) => ({
            ...point,
            value: point.value === null ? null : point.value * 100,
          })),
        },
      ],
      onOff: [
        { name: "On-court net", points: series.on_court_net },
        { name: "On/off net", points: series.on_off_net },
      ],
      shooting: [
        { name: "TS%", points: series.ts_pct },
        { name: "USG%", points: series.usg_pct },
      ],
      impact: series.impact.map((s) => ({ name: IMPACT_LABELS[s.source], points: s.points })),
    }),
    [series],
  );

  return (
    <div className="player-page">
      <section aria-labelledby="player-tiers" className="player-tiers">
        <h2 id="player-tiers">Tier</h2>
        {page.tiers.length === 0 ? (
          <p>No games in the window.</p>
        ) : (
          <TierStrip tiers={page.tiers} className="chart-tier-strip" />
        )}
      </section>
      <section aria-labelledby="player-minutes">
        <h2 id="player-minutes">Minutes &amp; share</h2>
        <TimeSeries series={charts.minutes} annotations={annotations} yFormat={oneDecimal} />
      </section>
      <section aria-labelledby="player-on-off">
        <h2 id="player-on-off">On/off net</h2>
        <p className="note">Per 100 possessions, garbage time {garbage}.</p>
        <TimeSeries series={charts.onOff} annotations={annotations} yFormat={signed} />
      </section>
      <section aria-labelledby="player-shooting">
        <h2 id="player-shooting">TS% &amp; USG%</h2>
        <TimeSeries series={charts.shooting} annotations={annotations} yFormat={percent} />
      </section>
      <section aria-labelledby="player-impact">
        <h2 id="player-impact">Impact</h2>
        <TimeSeries
          series={charts.impact}
          annotations={annotations}
          yFormat={signed}
          empty="No impact metric has a value for him yet."
        />
      </section>
      <section aria-labelledby="player-shots">
        <h2 id="player-shots">Shot profile</h2>
        <Shots zones={page.shot_profile} window={page.window} />
      </section>
      <section aria-labelledby="player-production">
        <h2 id="player-production">Production</h2>
        <p className="note">
          Every window at once, each with its difference from the season in small type, a percentage&apos;s in
          points.
        </p>
        <ProductionTable rows={page.production} />
      </section>
      <section aria-labelledby="player-teammates">
        <h2 id="player-teammates">Teammates</h2>
        <p className="note">Most possessions together in the window, garbage time {garbage}.</p>
        <Teammates teammates={page.teammates} />
      </section>
      <section aria-labelledby="player-matchups">
        <h2 id="player-matchups">Matchups</h2>
        <MatchupTable caption="Guarded by" rows={page.matchups.defenders} />
        <MatchupTable caption="Guarding" rows={page.matchups.guarded} />
      </section>
      <section aria-labelledby="player-flags">
        <h2 id="player-flags">Flag history</h2>
        <FlagList flags={page.flags} />
      </section>
    </div>
  );
}

/** A chart of the series with the page's annotations, or `empty` when none has a point. */
function TimeSeries({
  series,
  annotations,
  yFormat,
  empty = "No games in the window.",
}: {
  series: NamedSeries[];
  annotations: Schemas["Annotation"][];
  yFormat: (value: number) => string;
  empty?: string;
}) {
  if (series.every((s) => s.points.length === 0)) return <p>{empty}</p>;
  return <AnnotatedTimeSeries series={series} annotations={annotations} yFormat={yFormat} />;
}

/** The window's zones against the season's and the prior season's, leaving out a span with no shots. */
function Shots({ zones, window }: { zones: Schemas["ShotZone"][]; window: Window }) {
  const windows = useMemo(() => {
    const spans: ShotWindow[] = [
      // A season window is the season's, so it is drawn once.
      ...(window === "season"
        ? []
        : [{ label: WINDOW_LABELS[window], zones: zones.map(({ zone, freq, fg_pct }) => ({ zone, freq, fg_pct })) }]),
      {
        label: "Season",
        zones: zones.map((z) => ({ zone: z.zone, freq: z.season_freq, fg_pct: z.season_fg_pct })),
      },
      {
        label: "Prior season",
        zones: zones.map((z) => ({ zone: z.zone, freq: z.prior_freq, fg_pct: z.prior_fg_pct })),
      },
    ];
    return spans.filter((span) => span.zones.some((z) => z.freq !== null));
  }, [zones, window]);

  if (windows.length === 0) return <p>No shots yet.</p>;
  return <ShotProfile windows={windows} />;
}

function Teammates({ teammates }: { teammates: Schemas["Teammate"][] }) {
  if (teammates.length === 0) return <p>No shared minutes in the window.</p>;
  return (
    <table className="teammates">
      <thead>
        <tr>
          <th scope="col">Teammate</th>
          <th scope="col">Poss</th>
          <th scope="col">Net</th>
        </tr>
      </thead>
      <tbody>
        {teammates.map((teammate) => (
          <tr key={teammate.player.player_id}>
            <td>
              <PlayerLink player={teammate.player} />
            </td>
            <td className="number">{teammate.possessions}</td>
            <td className="number">{signed(teammate.net_rating)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Each matchup's offense over the window: his against a defender, or a player's against him. */
function MatchupTable({ caption, rows }: { caption: string; rows: Schemas["MatchupRow"][] }) {
  return (
    <table className="matchups">
      <caption>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">Player</th>
          <th scope="col">Partial poss</th>
          <th scope="col">Pts</th>
          <th scope="col">FG</th>
          <th scope="col">3P</th>
          <th scope="col">Ast</th>
          <th scope="col">Tov</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={7}>None in the window.</td>
          </tr>
        )}
        {rows.map((row) => (
          <tr key={row.player.player_id}>
            <td>
              <PlayerLink player={row.player} />
            </td>
            <td className="number">{row.partial_poss.toFixed(1)}</td>
            <td className="number">{row.pts}</td>
            <td className="number">
              {row.fgm}-{row.fga}
            </td>
            <td className="number">
              {row.fg3m}-{row.fg3a}
            </td>
            <td className="number">{row.ast}</td>
            <td className="number">{row.tov}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default PlayerPage;
