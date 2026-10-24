import type { components } from "../api/types";
import AnnotatedTimeSeries from "../components/charts/AnnotatedTimeSeries";
import ShotProfile from "../components/charts/ShotProfile";
import SparklineCell from "../components/charts/SparklineCell";
import StintTimeline from "../components/charts/StintTimeline";
import type { StintRow } from "../components/charts/StintTimeline";

// Every chart with made-up data, to eyeball them without a backend. Routed at `/dev/charts` in the
// dev server only; the production bundle leaves this module out (see `routes.tsx`).

type Schemas = components["schemas"];

const DATES = Array.from({ length: 20 }, (_, i) => {
  const date = new Date(Date.UTC(2026, 1, 20 + i * 2));
  return date.toISOString().slice(0, 10);
});

/** A smooth made-up series, deterministic so the page looks the same on every load. */
function wave(base: number, swing: number, phase: number): Schemas["Point"][] {
  return DATES.map((game_date, i) => ({ game_date, value: base + swing * Math.sin(i / 3 + phase) }));
}

const SPARKLINES = [
  { name: "Minutes share", series: wave(0.13, 0.03, 0), baseline: 0.12 },
  { name: "On/off net", series: wave(2, 6, 1), baseline: 0 },
  {
    name: "Impact",
    series: wave(1.5, 0.8, 2).map((point, i) => (i < 15 ? point : { ...point, value: null })),
  },
];

const ANNOTATIONS: Schemas["Annotation"][] = [
  { game_date: DATES[4], kind: "injury", label: "Available → Out (left ankle sprain)" },
  { game_date: DATES[8], kind: "injury", label: "Out → Available" },
  { game_date: DATES[8], kind: "starter", label: "Moved to the bench" },
  { game_date: DATES[13], kind: "transaction", label: "Traded to BOS" },
  { game_date: DATES[17], kind: "closer", label: "Closed the game" },
];

const TIMELINE_PLAYERS = [
  ["NYK", "Jalen Brunson"],
  ["NYK", "Josh Hart"],
  ["NYK", "Mikal Bridges"],
  ["NYK", "OG Anunoby"],
  ["NYK", "Karl-Anthony Towns"],
  ["NYK", "Miles McBride"],
  ["BOS", "Jayson Tatum"],
  ["BOS", "Jaylen Brown"],
  ["BOS", "Derrick White"],
  ["BOS", "Payton Pritchard"],
  ["BOS", "Neemias Queta"],
  ["BOS", "Sam Hauser"],
];

// Starters play the first 8 minutes of each period and the last 2, but for the fourth's; the bench
// the 2 between, and the fourth's last 2, which are garbage time.
const ROWS: StintRow[] = TIMELINE_PLAYERS.map(([team, label], i) => {
  const starter = i % 6 < 5;
  const stints = [1, 2, 3, 4].flatMap((period) =>
    starter
      ? [
          { period, start_sec: 0, end_sec: 480, is_garbage_time: false },
          ...(period < 4 ? [{ period, start_sec: 600, end_sec: 720, is_garbage_time: false }] : []),
        ]
      : [
          { period, start_sec: 480, end_sec: 600, is_garbage_time: false },
          ...(period === 4 ? [{ period, start_sec: 600, end_sec: 720, is_garbage_time: true }] : []),
        ],
  );
  return { team, label, stints };
});

const SHOT_WINDOWS = [
  { label: "Last 10", shift: 0.03 },
  { label: "Season", shift: 0 },
  { label: "Prior season", shift: -0.03 },
].map(({ label, shift }) => ({
  label,
  zones: [
    { zone: "rim" as const, freq: 0.34 + shift, fg_pct: 0.66 },
    { zone: "short_mid" as const, freq: 0.14, fg_pct: 0.44 + shift },
    { zone: "long_mid" as const, freq: 0.08 - shift, fg_pct: 0.41 },
    { zone: "corner_3" as const, freq: 0.12, fg_pct: 0.39 },
    { zone: "above_break_3" as const, freq: 0.32, fg_pct: 0.35 - shift },
  ],
}));

const percent = (value: number) => `${(value * 100).toFixed(1)}%`;

function ChartsPage() {
  return (
    <main className="shell">
      <h1>Charts</h1>

      <h2>SparklineCell</h2>
      <table>
        <tbody>
          {SPARKLINES.map(({ name, series, baseline }) => (
            <tr key={name}>
              <td>{name}</td>
              <td>
                <SparklineCell series={series} baseline={baseline} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>AnnotatedTimeSeries</h2>
      <AnnotatedTimeSeries
        series={[
          { name: "Minutes share", points: wave(0.13, 0.03, 0) },
          { name: "Usage", points: wave(0.22, 0.02, 1.5) },
        ]}
        annotations={ANNOTATIONS}
        yFormat={percent}
      />

      <h2>StintTimeline</h2>
      <StintTimeline rows={ROWS} periods={4} garbageTimeFrom={{ period: 4, start_sec: 600 }} />

      <h2>ShotProfile</h2>
      <ShotProfile windows={SHOT_WINDOWS} />
    </main>
  );
}

export default ChartsPage;
