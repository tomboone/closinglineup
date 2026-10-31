import type { components } from "../../api/types";

// The charts' shared colours, so a tier or an event kind reads the same on every page.

type Schemas = components["schemas"];

export const TIER_COLOURS: Record<Schemas["Tier"], string> = {
  starter: "#1f6feb",
  core: "#2da44e",
  fringe: "#bf8700",
  out: "#8c959f",
  injured: "#cf222e",
};

export const ANNOTATION_COLOURS: Record<Schemas["Annotation"]["kind"], string> = {
  injury: "#cf222e",
  transaction: "#8250df",
  tier: "#1b7c83",
  starter: "#1f6feb",
  closer: "#bc4c00",
};

/** One colour per series, in order, for charts drawing several. */
export const SERIES_COLOURS = ["#1f6feb", "#bc4c00", "#2da44e", "#8250df", "#57606a"];

/** A game's two teams, the first listed taking the first colour. */
export const TEAM_COLOURS = ["#0969da", "#cf6a00"];

export const AXIS_COLOUR = "#8c959f";

/** Text drawn on a chart, in the page's ink rather than a series colour. */
export const LABEL_COLOUR = "#1a1a1a";

export const GARBAGE_TIME_FILL = "rgba(140, 149, 159, 0.18)";

/** A mover's direction, the poles of a diverging pair: rising cool, falling warm. */
export const MOVER_COLOURS = { rising: "#0969da", falling: "#cf222e" };
