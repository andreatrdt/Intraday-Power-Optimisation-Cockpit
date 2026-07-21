def test_data_flow_endpoints_are_inspectable(client) -> None:
    feeds = client.get("/api/v1/data-sources/health")
    assert feeds.status_code == 200
    assert len(feeds.json()["feeds"]) == 9

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
