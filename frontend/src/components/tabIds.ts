/** The ids tying a tab list to its panel, from one `useId` prefix: each tab's, and the panel's. */
export function tabIds(prefix: string) {
  return { tab: (name: string) => `${prefix}-tab-${name}`, panel: `${prefix}-panel` };
}
