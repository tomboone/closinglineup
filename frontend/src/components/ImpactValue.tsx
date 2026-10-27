import type { components } from "../api/types";
import { signed } from "./format";
import { IMPACT_LABELS } from "./labels";

type Impact = components["schemas"]["Impact"];

/**
 * A feed row's impact (v2 spec §7): the player's latest value, signed, with its source named as the
 * team board names it; a dash when he has none.
 */
function ImpactValue({ impact }: { impact: Impact | null }) {
  return (
    <span className="impact">
      {signed(impact?.value)}
      {impact && <span className="impact-source">{IMPACT_LABELS[impact.source]}</span>}
    </span>
  );
}

export default ImpactValue;
