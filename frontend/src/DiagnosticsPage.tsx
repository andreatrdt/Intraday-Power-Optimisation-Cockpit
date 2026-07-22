import { ConnectionStatus } from "./ConnectionStatus";
import { ProductNav } from "./ProductNav";

const diagnostics = [
  ["Data Flow", "/data-flow", "Source ingestion, health, transformations and snapshot assembly."],
  ["Forecast & Position", "/forecast-position", "Forecast vintages, uncertainty and pre-action exposure."],
  ["Market & Liquidity", "/market-liquidity", "Executable book depth, WAP and Gate Closure diagnostics."],
  ["Battery Flexibility", "/battery-flexibility", "Current physical envelope and opportunity cost."],
  ["Battery Path", "/battery-path", "Sequential feasibility and standard path diagnostics."],
  ["Optionality", "/optionality", "BM and ancillary-service value diagnostics."],
  ["Legacy Coordinator", "/coordinator", "Earlier fixed-candidate coordinator retained for comparison."],
];

export function DiagnosticsPage() {
  return <div className="app-shell diagnostics-home">
    <header className="topbar"><div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>Diagnostics</h1></div></div><ProductNav active="diagnostics" /><ConnectionStatus error={false} lastPoll={new Date()} /></header>
    <main>
      <section className="hero-row"><div><p className="eyebrow">SECONDARY TECHNICAL VIEWS</p><h2>Inspect the layers behind the rolling decision.</h2><p className="intro">These pages consume the current rolling-state snapshot. They support the product; they are not separate trading products.</p></div></section>
      <section className="diagnostic-directory">{diagnostics.map(([label, href, description]) => <a className="panel diagnostic-card" href={href} key={href}><span>DIAGNOSTIC</span><h3>{label}</h3><p>{description}</p><strong>Open view →</strong></a>)}</section>
    </main>
  </div>;
}
