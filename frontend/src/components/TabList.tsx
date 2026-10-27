import { tabIds } from "./tabIds";

/**
 * A row of tabs over one panel, as the WAI-ARIA tabs pattern has them: each tab a button, the
 * selected one `aria-selected`. The page renders the panel itself, with `tabIds(prefix).panel` for
 * its id and the selected tab's id for its label.
 */
function TabList<T extends string>({
  label,
  prefix,
  tabs,
  labels,
  selected,
  onSelect,
}: {
  label: string;
  prefix: string;
  tabs: readonly T[];
  labels: Record<T, string>;
  selected: T;
  onSelect: (tab: T) => void;
}) {
  const ids = tabIds(prefix);
  return (
    <div role="tablist" aria-label={label} className="tab-list">
      {tabs.map((one) => (
        <button
          key={one}
          type="button"
          role="tab"
          id={ids.tab(one)}
          aria-selected={one === selected}
          aria-controls={ids.panel}
          onClick={() => onSelect(one)}
        >
          {labels[one]}
        </button>
      ))}
    </div>
  );
}

export default TabList;
