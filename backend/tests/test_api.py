def test_data_flow_endpoints_are_inspectable(client) -> None:
    feeds = client.get("/api/v1/data-sources/health")
    assert feeds.status_code == 200
    assert len(feeds.json()["feeds"]) == 11

    snapshot = client.get("/api/v1/snapshots/current")
    assert snapshot.status_code == 200
    body = snapshot.json()["snapshot"]
    assert body["status"] == "DEGRADED"
    assert body["optimiser_readiness"]["status"] == "BLOCKED"

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
    assert len(current.json()["periods"]) == 8


def test_battery_flexibility_endpoint_and_lineage(client) -> None:
    response = client.get("/api/v1/battery-flexibility")
    assert response.status_code == 200
    battery = response.json()["battery"]
    assert battery["source_mode"] == "SAMPLE"
    assert battery["readiness"]["status"] == "DEGRADED"
    assert len(battery["periods"]) == 8
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
