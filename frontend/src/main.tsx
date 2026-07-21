import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ForecastPositionPage } from "./ForecastPositionPage";
import { MarketLiquidityPage } from "./MarketLiquidityPage";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {window.location.pathname.startsWith("/market-liquidity") ? <MarketLiquidityPage /> : window.location.pathname.startsWith("/forecast-position") ? <ForecastPositionPage /> : <App />}
  </React.StrictMode>,
);
