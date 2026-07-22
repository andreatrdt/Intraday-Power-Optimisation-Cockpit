export function ProductNav({ active }: { active: "live" | "optimisation" | "diagnostics" }) {
  return <nav className="product-nav" aria-label="Primary navigation">
    <a className={active === "live" ? "active" : ""} href="/live">Live State</a>
    <a className={active === "optimisation" ? "active" : ""} href="/optimisation">Optimisation</a>
    <a className={active === "diagnostics" ? "active" : ""} href="/diagnostics">Diagnostics</a>
    {active === "diagnostics" && <span className="diagnostic-context">Subview of current rolling state</span>}
  </nav>;
}
