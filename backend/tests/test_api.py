def test_data_flow_endpoints_are_inspectable(client) -> None:
    feeds = client.get("/api/v1/data-sources/health")
    assert feeds.status_code == 200
    assert len(feeds.json()["feeds"]) == 11

    snapshot = client.get("/api/v1/snapshots/current")
    assert snapshot.status_code == 200
    body = snapshot.json()["snapshot"]
    assert body["status"] == "DEGRADED"
    assert body["optimiser_readiness"]["status"] == "DEGRADED"
    assert body["optimiser_readiness"]["allowed"] is True

    value_id = body["values"][0]["value_id"]
    lineage = client.get(f"/api/v1/lineage/{value_id}")
    assert lineage.status_code == 200
    assert lineage.json()["value"]["lineage"]["source_feed"]


def test_unconfigured_market_refresh_surfaces_failure(client) -> None:
    response = client.post("/api/v1/data-sources/market_intraday/refresh", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["attempt"]["status"] == "FAILED"
    assert body["feed"]["source_mode"] == "ERROR"
    assert "No licensed intraday market provider" in body["feed"]["latest_error_message"]


def test_forecast_position_endpoints_and_derived_lineage(client) -> None:
    response = client.get("/api/v1/forecast-position")
    assert response.status_code == 200
    snapshot = response.json()["forecast_position"]
    assert snapshot["readiness"]["status"] == "DEGRADED"
    assert snapshot["periods"]

    exposure = snapshot["periods"][0]["exposures"][1]["exposure_value"]
    lineage = client.get(f"/api/v1/lineage/{exposure['value_id']}")
    assert lineage.status_code == 200
    assert lineage.json()["value"]["lineage"]["source_feed"] == "forecast_position_calculation"

    forecasts = client.get("/api/v1/forecasts/current")
    positions = client.get("/api/v1/positions/current")
    assert forecasts.status_code == 200
    assert positions.status_code == 200
    assert len(forecasts.json()["forecasts"]) == len(positions.json()["positions"])


def test_market_liquidity_endpoint_and_wap_lineage(client) -> None:
    response = client.get("/api/v1/market-liquidity")
    assert response.status_code == 200
    market = response.json()["market"]
    assert market["source_mode"] == "SAMPLE"
    assert market["live_provider_status"] == "ERROR"
    assert market["readiness"]["status"] == "DEGRADED"
    hedge = market["periods"][0]["p50_hedge"]
    if hedge["execution"]["wap_value"]:
        value_id = hedge["execution"]["wap_value"]["value_id"]
        lineage = client.get(f"/api/v1/lineage/{value_id}")
        assert lineage.status_code == 200
        assert lineage.json()["value"]["lineage"]["source_feed"] == "market_liquidity_calculation"

    current = client.get("/api/v1/markets/current")
    assert current.status_code == 200
    assert len(current.json()["periods"]) > 0


def test_battery_flexibility_endpoint_and_lineage(client) -> None:
    response = client.get("/api/v1/battery-flexibility")
    assert response.status_code == 200
    battery = response.json()["battery"]
    assert battery["source_mode"] == "SAMPLE"
    assert battery["readiness"]["status"] == "DEGRADED"
    assert len(battery["periods"]) > 0
    value_id = battery["periods"][0]["feasibility"]["max_discharge_value"]["value_id"]
    lineage = client.get(f"/api/v1/lineage/{value_id}")
    assert lineage.status_code == 200
    assert lineage.json()["value"]["lineage"]["source_feed"] == "battery_flexibility_calculation"

    current = client.get("/api/v1/batteries/current")
    assert current.status_code == 200
    assert current.json()["current_soc"]["unit"] == "MWh"


def test_battery_path_comparison_custom_simulation_and_lineage(client) -> None:
    comparison_response = client.get("/api/v1/battery-paths/comparison")
    assert comparison_response.status_code == 200
    comparison = comparison_response.json()["comparison"]
    assert comparison["no_action"]["path_name"] == "NO_ACTION"
    assert comparison["p50_coverage"]["diagnostic_only"] is True

    first_period = comparison["no_action"]["periods"][0]["delivery_period"]
    custom_response = client.post("/api/v1/battery-paths/simulate", json={
        "path_name": "CUSTOM",
        "actions": [{"delivery_period": first_period, "charge_mw": 4, "discharge_mw": 0}],
    })
    assert custom_response.status_code == 200
    custom = custom_response.json()["simulation"]
    assert custom["path_name"] == "CUSTOM"
    assert custom["periods"][1]["starting_soc_mwh"] == custom["periods"][0]["ending_soc_mwh"]

    value_id = custom["periods"][0]["ending_soc_value"]["value_id"]
    lineage = client.get(f"/api/v1/lineage/{value_id}")
    assert lineage.status_code == 200
    assert lineage.json()["value"]["lineage"]["source_feed"] == "battery_path_simulation"


def test_optionality_standard_custom_and_lineage_endpoints(client) -> None:
    response = client.get("/api/v1/optionality")
    assert response.status_code == 200
    optionality = response.json()["optionality"]
    assert optionality["readiness"]["status"] == "DEGRADED"
    assert optionality["optional_not_guaranteed"] is True
    assert {item["path_name"] for item in optionality["path_impacts"]} == {
        "NO_ACTION", "P50_COVERAGE", "PRESERVE_FLEXIBILITY"
    }
    periods = optionality["path_impacts"][0]["periods"]
    custom_response = client.post("/api/v1/optionality/simulate", json={
        "path_name": "CUSTOM",
        "actions": [{
            "delivery_period": periods[0]["delivery_period"],
            "charge_mw": 10,
            "discharge_mw": 0,
        }],
    })
    assert custom_response.status_code == 200
    custom = custom_response.json()["optionality"]
    custom_impact = next(item for item in custom["path_impacts"] if item["path_name"] == "CUSTOM")
    assert custom_impact["periods"][1]["starting_soc_mwh"] > periods[1]["starting_soc_mwh"]
    value_id = custom_impact["periods"][0]["optionality_lost_value"]["value_id"]
    lineage = client.get(f"/api/v1/lineage/{value_id}")
    assert lineage.status_code == 200
    assert lineage.json()["value"]["lineage"]["source_feed"] == "optionality_diagnostic"


def test_coordinator_current_historic_simulation_and_lineage_endpoints(client) -> None:
    response = client.get("/api/v1/coordinator")
    assert response.status_code == 200
    coordinator = response.json()["coordinator"]
    assert coordinator["readiness"]["status"] == "DEGRADED"
    assert len(coordinator["candidates"]) == 6
    assert coordinator["recommendation"]["label"] == "Diagnostic recommendation"
    assert coordinator["recommendation"]["not_executable"] is True

    historic = client.get(f"/api/v1/coordinator/{coordinator['cockpit_snapshot_id']}")
    assert historic.status_code == 200
    assert historic.json()["coordinator"]["cockpit_snapshot_id"] == coordinator["cockpit_snapshot_id"]

    simulation = client.post("/api/v1/coordinator/simulate", json={
        "imbalance_price_gbp_per_mwh": 175,
        "tail_risk_weight": 0.8,
        "optionality_loss_weight": 2,
        "maximum_market_hedge_volume_mwh": 3,
        "selected_battery_path": "P50_COVERAGE",
        "confidence_scenario": "P10",
        "explicit_sample_market": True,
        "assumption_source_mode": "SAMPLE",
    })
    assert simulation.status_code == 200
    simulated = simulation.json()["coordinator"]
    assert next(item for item in simulated["assumptions"] if item["metric"] == "coordinator_confidence_scenario")["value"] == "P10"

    value_id = coordinator["recommendation"]["diagnostic_score_value"]["value_id"]
    lineage = client.get(f"/api/v1/lineage/{value_id}")
    assert lineage.status_code == 200
    assert lineage.json()["value"]["lineage"]["source_feed"] == "integrated_coordinator"


def test_rolling_state_and_optimisation_lifecycle_endpoints(client) -> None:
    reset = client.post("/api/v1/live-state/reset")
    assert reset.status_code == 200
    initial = reset.json()
    assert initial["live_state"]["state"]["state_source_mode"] == "SAMPLE"
    assert initial["optimisation"]["solver_status"] == "optimal"

    refreshed = client.post("/api/v1/live-state/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["live_state"]["state"]["current_forecast_vintage_id"] != initial["live_state"]["state"]["current_forecast_vintage_id"]
    assert refreshed.json()["optimisation"]["run_id"] != initial["optimisation"]["run_id"]

    regime = client.post("/api/v1/live-state/regime", json={"regime": "tightening"})
    assert regime.status_code == 200
    assert regime.json()["live_state"]["state"]["current_regime"] == "tightening"

    solved = client.post("/api/v1/optimisation/run")
    assert solved.status_code == 200
    run_id = solved.json()["optimisation"]["run_id"]
    assert solved.json()["optimisation"]["not_executable"] is True

    horizon = client.post("/api/v1/live-state/horizon", json={"mode": "next_auction"})
    assert horizon.status_code == 200
    assert horizon.json()["live_state"]["state"]["horizon_warning"] is None
    assert horizon.json()["optimisation"]["starting_state"]["effective_horizon_mode"] == "next_auction"

    runs = client.get("/api/v1/optimisation/runs")
    historical = client.get(f"/api/v1/optimisation/runs/{run_id}")
    current = client.get("/api/v1/optimisation/current")
    assert runs.status_code == historical.status_code == current.status_code == 200
    assert any(item["run_id"] == run_id for item in runs.json()["runs"])

    point = historical.json()["optimisation"]["lineage_values"][0]
    lineage = client.get(f"/api/v1/lineage/{point['value_id']}")
    assert lineage.status_code == 200
    assert lineage.json()["value"]["lineage"]["source_feed"] == "full_action_optimiser"
