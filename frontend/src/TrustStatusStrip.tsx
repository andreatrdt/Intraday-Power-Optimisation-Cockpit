import { Badge } from "./App";
import type { RollingState } from "./types";

export function TrustStatusStrip({ state, warnings = [] }: { state: RollingState; warnings?: string[] }) {
  const blocked = !state.trust.calculation_allowed;
  const horizonText = state.auction_calendar_configured
    ? `Auction-calendar horizon: ${state.effective_horizon_mode.replaceAll("_", " ")}.`
    : "Next-auction calendar not configured. Current horizon: next 8 SPs.";
  return <section className={`trust-status-strip ${blocked ? "blocked" : ""}`} aria-label="Data trust and calculation status">
    <div className="trust-status-badges">
      <Badge value={state.state_source_mode} />
      <span className="status-chip neutral">{state.trust.trustworthy_for_live_trading ? "LIVE" : "NOT LIVE"}</span>
      <span className={`status-chip ${state.trust.calculation_allowed ? "allowed" : "blocked"}`}>{state.trust.calculation_allowed ? "CALCULATION ALLOWED" : "CALCULATION BLOCKED"}</span>
      <span className="status-chip neutral">LIVE TRUST: {state.trust.trustworthy_for_live_trading ? "YES" : "NO"}</span>
      {!state.auction_calendar_configured && <span className="status-chip warning">AUCTION CALENDAR: NOT CONFIGURED</span>}
    </div>
    <p><strong>SAMPLE simulation:</strong> assumes previous model actions are followed. Not real execution.</p>
    <details>
      <summary>{horizonText}</summary>
      <p>The fallback uses the next eight GB settlement periods until a verified auction calendar is configured.</p>
      {warnings.map((warning) => <p key={warning}>{warning}</p>)}
    </details>
  </section>;
}
