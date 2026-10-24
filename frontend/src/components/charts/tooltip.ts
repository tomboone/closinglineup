// Tooltips are HTML, and their text (player names, injury and transaction labels) comes from
// scraped sources, so it is escaped before ECharts sets it as markup.

const ENTITIES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, (char) => ENTITIES[char]);
}

/** A coloured dot to lead a tooltip line, as ECharts' own markers do. */
export function dot(colour: string) {
  return `<span style="display:inline-block;margin-right:4px;width:8px;height:8px;border-radius:50%;background:${colour}"></span>`;
}
