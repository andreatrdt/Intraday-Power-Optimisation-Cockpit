import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ForecastPositionPage } from "./ForecastPositionPage";
import { MarketLiquidityPage } from "./MarketLiquidityPage";
import { BatteryFlexibilityPage } from "./BatteryFlexibilityPage";
import { BatteryPathPage } from "./BatteryPathPage";
import { OptionalityPage } from "./OptionalityPage";
import { CoordinatorPage } from "./CoordinatorPage";
import { DiagnosticsPage } from "./DiagnosticsPage";
import { LiveStatePage } from "./LiveStatePage";
import { OptimisationPage } from "./OptimisationPage";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {window.location.pathname.startsWith("/live") || window.location.pathname === "/" ? <LiveStatePage /> : window.location.pathname.startsWith("/optimisation") ? <OptimisationPage /> : window.location.pathname.startsWith("/diagnostics") ? <DiagnosticsPage /> : window.location.pathname.startsWith("/coordinator") ? <CoordinatorPage /> : window.location.pathname.startsWith("/optionality") ? <OptionalityPage /> : window.location.pathname.startsWith("/battery-path") ? <BatteryPathPage /> : window.location.pathname.startsWith("/battery-flexibility") ? <BatteryFlexibilityPage /> : window.location.pathname.startsWith("/market-liquidity") ? <MarketLiquidityPage /> : window.location.pathname.startsWith("/forecast-position") ? <ForecastPositionPage /> : <App />}
  </React.StrictMode>,
);
