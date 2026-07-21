"""Feed adapters with deliberately explicit source modes.

No adapter calls another adapter as a fallback. A live adapter raises on failure;
the pipeline records that failure and retains any prior value only as
LATEST_AVAILABLE/STALE.
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from cockpit.models import Quality, SemanticKind, SourceMode, ValidationCheck
from cockpit.settlement import UTC, upcoming_periods


@dataclass
class NormalisedValue:
    metric: str
    value: float | int | str | bool
    unit: str
    raw_field_name: str
    published_at: datetime | None = None
    delivery_period: str | None = None
    delivery_start: datetime | None = None
    transformations: list[str] = field(default_factory=list)
    checks: list[ValidationCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RawFeedResult:
    rows: list[dict[str, Any]]
    retrieved_at: datetime


class FeedAdapter(ABC):
    feed_id: str
    feed_name: str
    description: str
    source_mode: SourceMode
    semantic_kind: SemanticKind
    cadence_seconds: int
    freshness_sla_seconds: int
    configured: bool = True
    required_for_snapshot: bool = False
    required_for_optimiser: bool = False
    include_by_default: bool = True

    @abstractmethod
    async def fetch(self, now: datetime) -> RawFeedResult:
        raise NotImplementedError

    @abstractmethod
    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        raise NotImplementedError


class ElexonSystemAdapter(FeedAdapter):
    feed_id = "elexon_system"
    feed_name = "Elexon / BMRS system frequency"
    description = "Latest public GB system-frequency observations from Elexon Insights."
    source_mode = SourceMode.LIVE
    semantic_kind = SemanticKind.OBSERVATION
    cadence_seconds = 60
    freshness_sla_seconds = 180

    async def fetch(self, now: datetime) -> RawFeedResult:
        start = now - timedelta(minutes=10)
        params = {"from": start.isoformat(), "to": now.isoformat()}
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "intraday-power-cockpit/0.1"}) as client:
            response = await client.get(
                "https://data.elexon.co.uk/bmrs/api/v1/system/frequency", params=params
            )
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Elexon returned no system-frequency observations")
        return RawFeedResult(rows=rows, retrieved_at=now)

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        latest = max(
            result.rows,
            key=lambda row: row.get("measurementTime") or row.get("startTime") or "",
        )
        raw = latest.get("frequency")
        value = float(raw)
        published = _parse_time(latest.get("publishTime"))
        observed = _parse_time(latest.get("measurementTime") or latest.get("startTime"))
        checks = [
            ValidationCheck(name="numeric", passed=math.isfinite(value), detail="finite Hz value"),
            ValidationCheck(
                name="plausible_frequency",
                passed=45 <= value <= 55,
                detail="expected broad physical range 45-55 Hz",
            ),
        ]
        return [
            NormalisedValue(
                metric="gb_system_frequency",
                value=round(value, 4),
                unit="Hz",
                raw_field_name="frequency",
                published_at=published or observed,
                transformations=["selected latest measurementTime", "parsed numeric Hz", "rounded to 4 dp"],
                checks=checks,
            )
        ]


class NesoSystemAdapter(FeedAdapter):
    feed_id = "neso_system"
    feed_name = "NESO system data catalogue"
    description = "Live CKAN discovery of NESO datasets matching system-data topics."
    source_mode = SourceMode.LIVE
    semantic_kind = SemanticKind.OBSERVATION
    cadence_seconds = 900
    freshness_sla_seconds = 1800

    async def fetch(self, now: datetime) -> RawFeedResult:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "intraday-power-cockpit/0.1"}) as client:
            response = await client.get(
                "https://api.neso.energy/api/3/action/package_search",
                params={"q": "system", "rows": 10},
            )
            response.raise_for_status()
            payload = response.json()
        if not payload.get("success"):
            raise RuntimeError("NESO CKAN package_search reported success=false")
        result = payload.get("result", {})
        rows = result.get("results", [])
        return RawFeedResult(
            rows=[{"count": result.get("count", len(rows)), "results": rows}], retrieved_at=now
        )

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        count = int(result.rows[0].get("count", 0))
        return [
            NormalisedValue(
                metric="neso_system_dataset_matches",
                value=count,
                unit="datasets",
                raw_field_name="result.count",
                published_at=result.retrieved_at,
                transformations=["CKAN package_search q=system", "extracted result.count"],
                checks=[
                    ValidationCheck(
                        name="non_empty_catalogue",
                        passed=count > 0,
                        detail="at least one NESO system dataset discovered",
                    )
                ],
                warnings=["Catalogue connectivity signal; not an executable market input."],
            )
        ]


class SampleForecastAdapter(FeedAdapter):
    feed_id = "forecast_sample"
    feed_name = "Renewable generation forecast"
    description = "Explicit sample P10/P50/P90 renewable forecast for eight upcoming periods."
    source_mode = SourceMode.SAMPLE
    semantic_kind = SemanticKind.FORECAST
    cadence_seconds = 900
    freshness_sla_seconds = 1800
    required_for_snapshot = True
    required_for_optimiser = True

    async def fetch(self, now: datetime) -> RawFeedResult:
        rows = []
        previous_shifts = (2.0, -1.0, 4.0, 8.0, -3.0, 5.0, -2.0, 1.0)
        for index, period in enumerate(upcoming_periods(now, 8)):
            p50 = 70 + 9 * math.sin((index + 1) / 2.2)
            previous_p50 = p50 + previous_shifts[index]
            rows.append(
                {
                    "delivery_period": period.label,
                    "delivery_start": period.start_utc.isoformat(),
                    "wind_p10_mwh": round(p50 * 0.78, 2),
                    "wind_p50_mwh": round(p50, 2),
                    "wind_p90_mwh": round(p50 * 1.22, 2),
                    "previous_wind_p50_mwh": round(previous_p50, 2),
                    "day_ahead_wind_p50_mwh": round(p50 + 5 * math.cos(index / 2), 2),
                    "model_disagreement_mwh": round(3.5 + index * 0.55, 2),
                    "reliability_score": round(max(0.62, 0.88 - index * 0.025), 3),
                    "issued_at": now.isoformat(),
                    "previous_issued_at": (now - timedelta(minutes=15)).isoformat(),
                    "day_ahead_issued_at": (now - timedelta(hours=6)).isoformat(),
                }
            )
        return RawFeedResult(rows=rows, retrieved_at=now)

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        values: list[NormalisedValue] = []
        for row in result.rows:
            quantile_order_valid = (
                float(row["wind_p10_mwh"])
                <= float(row["wind_p50_mwh"])
                <= float(row["wind_p90_mwh"])
            )
            fields = (
                ("wind_p10", "wind_p10_mwh", row["issued_at"], "latest P10"),
                ("wind_p50", "wind_p50_mwh", row["issued_at"], "latest P50"),
                ("wind_p90", "wind_p90_mwh", row["issued_at"], "latest P90"),
                (
                    "wind_previous_p50",
                    "previous_wind_p50_mwh",
                    row["previous_issued_at"],
                    "previous-vintage P50",
                ),
                (
                    "wind_day_ahead_p50",
                    "day_ahead_wind_p50_mwh",
                    row["day_ahead_issued_at"],
                    "day-ahead baseline P50",
                ),
                (
                    "wind_model_disagreement",
                    "model_disagreement_mwh",
                    row["issued_at"],
                    "model disagreement",
                ),
                (
                    "wind_reliability_score",
                    "reliability_score",
                    row["issued_at"],
                    "forecast reliability score",
                ),
            )
            for metric, field_name, published_at, label in fields:
                value = float(row[field_name])
                unit = "score" if metric == "wind_reliability_score" else "MWh"
                checks = [
                    ValidationCheck(
                        name="quantile_order",
                        passed=quantile_order_valid,
                        detail="P10 <= P50 <= P90",
                    )
                ]
                if metric == "wind_reliability_score":
                    checks.append(
                        ValidationCheck(
                            name="unit_interval",
                            passed=0 <= value <= 1,
                            detail="0 <= reliability score <= 1",
                        )
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            name="non_negative",
                            passed=value >= 0,
                            detail="forecast quantity >= 0",
                        )
                    )
                values.append(
                    NormalisedValue(
                        metric=metric,
                        value=value,
                        unit=unit,
                        raw_field_name=field_name,
                        published_at=_parse_time(published_at),
                        delivery_period=row["delivery_period"],
                        delivery_start=_parse_time(row["delivery_start"]),
                        transformations=[
                            "sample forecast vintage row",
                            f"mapped {label} to canonical metric",
                        ],
                        checks=checks,
                        warnings=["Sample forecast: not supplied by a live private forecasting system."],
                    )
                )
        return values


class SamplePositionAdapter(FeedAdapter):
    feed_id = "portfolio_position_sample"
    feed_name = "Portfolio contracted position"
    description = "Explicit sample contracted net-export position Q_t. Positive means sold/export."
    source_mode = SourceMode.SAMPLE
    semantic_kind = SemanticKind.ASSUMPTION
    cadence_seconds = 300
    freshness_sla_seconds = 900
    required_for_snapshot = True
    required_for_optimiser = True

    async def fetch(self, now: datetime) -> RawFeedResult:
        return RawFeedResult(
            rows=[
                {
                    "delivery_period": period.label,
                    "delivery_start": period.start_utc.isoformat(),
                    "contracted_net_export_mwh": round(68 + 2 * math.cos(index), 2),
                }
                for index, period in enumerate(upcoming_periods(now, 8))
            ],
            retrieved_at=now,
        )

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        return [
            NormalisedValue(
                metric="contracted_position_q",
                value=float(row["contracted_net_export_mwh"]),
                unit="MWh",
                raw_field_name="contracted_net_export_mwh",
                published_at=result.retrieved_at,
                delivery_period=row["delivery_period"],
                delivery_start=_parse_time(row["delivery_start"]),
                transformations=["sample private book row", "applied positive-export sign convention"],
                checks=[ValidationCheck(name="finite", passed=True, detail="finite signed MWh")],
                warnings=["Sample portfolio position: not connected to a live ETRM/trading book."],
            )
            for row in result.rows
        ]


class SampleBatteryAdapter(FeedAdapter):
    feed_id = "battery_telemetry_sample"
    feed_name = "Battery telemetry"
    description = "Explicit sample battery SoC; no asset telemetry is connected."
    source_mode = SourceMode.SAMPLE
    semantic_kind = SemanticKind.OBSERVATION
    cadence_seconds = 30
    freshness_sla_seconds = 90
    required_for_snapshot = True
    required_for_optimiser = True

    async def fetch(self, now: datetime) -> RawFeedResult:
        return RawFeedResult(rows=[{"state_of_charge_mwh": 54.2, "measured_at": now.isoformat()}], retrieved_at=now)

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        row = result.rows[0]
        value = float(row["state_of_charge_mwh"])
        return [
            NormalisedValue(
                metric="battery_soc",
                value=value,
                unit="MWh",
                raw_field_name="state_of_charge_mwh",
                published_at=_parse_time(row["measured_at"]),
                transformations=["sample telemetry row", "mapped to usable stored energy"],
                checks=[ValidationCheck(name="soc_bounds", passed=0 <= value <= 100, detail="0 <= SoC <= 100 MWh")],
                warnings=["Sample telemetry: must not be treated as a confirmed physical state."],
            )
        ]


class SampleBatteryConfigAdapter(FeedAdapter):
    feed_id = "battery_config_sample"
    feed_name = "Battery operating limits"
    description = "Explicit sample battery limits and diagnostic cost assumptions."
    source_mode = SourceMode.SAMPLE
    semantic_kind = SemanticKind.ASSUMPTION
    cadence_seconds = 3600
    freshness_sla_seconds = 7200
    required_for_snapshot = True
    required_for_optimiser = True

    async def fetch(self, now: datetime) -> RawFeedResult:
        return RawFeedResult(
            rows=[{
                "e_min_mwh": 10.0,
                "e_max_mwh": 100.0,
                "charge_max_mw": 20.0,
                "discharge_max_mw": 20.0,
                "charge_efficiency": 0.94,
                "discharge_efficiency": 0.92,
                "reserve_duration_hours": 1.0,
                "terminal_soc_target_mwh": 55.0,
                "degradation_cost_gbp_per_mwh": 4.0,
                "terminal_soc_penalty_gbp_per_mwh": 1.5,
                "future_flexibility_penalty_gbp_per_mwh": 2.5,
            }],
            retrieved_at=now,
        )

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        row = result.rows[0]
        definitions = (
            ("battery_e_min", "e_min_mwh", "MWh", lambda value: value >= 0, "E_min >= 0"),
            ("battery_e_max", "e_max_mwh", "MWh", lambda value: value > 0, "E_max > 0"),
            ("battery_charge_power_max", "charge_max_mw", "MW", lambda value: value >= 0, "P_charge_max >= 0"),
            ("battery_discharge_power_max", "discharge_max_mw", "MW", lambda value: value >= 0, "P_discharge_max >= 0"),
            ("battery_charge_efficiency", "charge_efficiency", "ratio", lambda value: 0 < value <= 1, "0 < eta_c <= 1"),
            ("battery_discharge_efficiency", "discharge_efficiency", "ratio", lambda value: 0 < value <= 1, "0 < eta_d <= 1"),
            ("battery_reserve_duration", "reserve_duration_hours", "h", lambda value: value >= 0, "h >= 0"),
            ("battery_terminal_soc_target", "terminal_soc_target_mwh", "MWh", lambda value: value >= 0, "terminal target >= 0"),
            ("battery_degradation_cost", "degradation_cost_gbp_per_mwh", "GBP/MWh", lambda value: value >= 0, "degradation cost >= 0"),
            ("battery_terminal_soc_penalty", "terminal_soc_penalty_gbp_per_mwh", "GBP/MWh", lambda value: value >= 0, "terminal penalty >= 0"),
            ("battery_future_flexibility_penalty", "future_flexibility_penalty_gbp_per_mwh", "GBP/MWh", lambda value: value >= 0, "future flexibility penalty >= 0"),
        )
        values: list[NormalisedValue] = []
        for metric, field_name, unit, valid, detail in definitions:
            value = float(row[field_name])
            values.append(
                NormalisedValue(
                    metric=metric,
                    value=value,
                    unit=unit,
                    raw_field_name=field_name,
                    published_at=result.retrieved_at,
                    transformations=["sample asset configuration", "mapped to canonical battery parameter"],
                    checks=[ValidationCheck(name="valid_parameter", passed=valid(value), detail=detail)],
                    warnings=["Sample battery configuration: replace with approved asset limits before live use."],
                )
            )
        return values


class SampleServiceAdapter(FeedAdapter):
    feed_id = "service_commitments_sample"
    feed_name = "Service commitments"
    description = "Explicit sample upward/downward capacity commitments."
    source_mode = SourceMode.SAMPLE
    semantic_kind = SemanticKind.ASSUMPTION
    cadence_seconds = 1800
    freshness_sla_seconds = 3600

    async def fetch(self, now: datetime) -> RawFeedResult:
        return RawFeedResult(rows=[{"upward_reserved_mw": 8.0, "downward_reserved_mw": 5.0}], retrieved_at=now)

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        row = result.rows[0]
        values = []
        for metric, field_name in (
            ("upward_service_commitment", "upward_reserved_mw"),
            ("downward_service_commitment", "downward_reserved_mw"),
        ):
            value = float(row[field_name])
            values.append(
                NormalisedValue(
                    metric=metric,
                    value=value,
                    unit="MW",
                    raw_field_name=field_name,
                    published_at=result.retrieved_at,
                    transformations=["sample commitment row", "mapped direction to canonical metric"],
                    checks=[ValidationCheck(name="non_negative", passed=value >= 0, detail="reserved MW >= 0")],
                    warnings=["Sample service commitment: not connected to a live contract source."],
                )
            )
        return values


class UnconfiguredMarketAdapter(FeedAdapter):
    feed_id = "market_intraday"
    feed_name = "Intraday executable market"
    description = "Licensed bid/ask and order-book depth feed required for executable decisions."
    source_mode = SourceMode.ERROR
    semantic_kind = SemanticKind.OBSERVATION
    cadence_seconds = 5
    freshness_sla_seconds = 15
    configured = False
    required_for_optimiser = True
    include_by_default = False

    async def fetch(self, now: datetime) -> RawFeedResult:
        raise RuntimeError(
            "No licensed intraday market provider is configured; Elexon MID is not treated as executable bid/ask data"
        )

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        return []


class SampleMarketOrderBookAdapter(FeedAdapter):
    feed_id = "market_order_book_sample"
    feed_name = "Sample intraday order book"
    description = "Explicit sample bid/ask levels for executable-price demonstrations only."
    source_mode = SourceMode.SAMPLE
    semantic_kind = SemanticKind.OBSERVATION
    cadence_seconds = 30
    freshness_sla_seconds = 90

    async def fetch(self, now: datetime) -> RawFeedResult:
        rows: list[dict[str, Any]] = []
        for period_index, period in enumerate(upcoming_periods(now, 8)):
            mid = 72.0 + period_index * 1.35 + 2.2 * math.sin(period_index / 1.7)
            spread = 1.2 + 0.25 * (period_index % 4)
            for level in range(1, 6):
                bid_volume = 2.0 + ((period_index + level) % 4) + 0.5 * level
                ask_volume = 1.5 + ((period_index * 2 + level) % 3) + 0.4 * level
                rows.extend(
                    [
                        {
                            "delivery_period": period.label,
                            "delivery_start": period.start_utc.isoformat(),
                            "side": "bid",
                            "level": level,
                            "price_gbp_mwh": round(mid - spread / 2 - 0.55 * (level - 1), 2),
                            "volume_mwh": round(bid_volume, 2),
                            "published_at": now.isoformat(),
                        },
                        {
                            "delivery_period": period.label,
                            "delivery_start": period.start_utc.isoformat(),
                            "side": "ask",
                            "level": level,
                            "price_gbp_mwh": round(mid + spread / 2 + 0.62 * (level - 1), 2),
                            "volume_mwh": round(ask_volume, 2),
                            "published_at": now.isoformat(),
                        },
                    ]
                )
        return RawFeedResult(rows=rows, retrieved_at=now)

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        values: list[NormalisedValue] = []
        warning = (
            "Sample order book: demonstration data, not a live executable market feed."
        )
        for row in result.rows:
            side = str(row["side"])
            level = int(row["level"])
            price = float(row["price_gbp_mwh"])
            volume = float(row["volume_mwh"])
            common = {
                "published_at": _parse_time(row["published_at"]),
                "delivery_period": row["delivery_period"],
                "delivery_start": _parse_time(row["delivery_start"]),
                "warnings": [warning],
            }
            values.append(
                NormalisedValue(
                    metric=f"market_{side}_price_l{level}",
                    value=price,
                    unit="GBP/MWh",
                    raw_field_name="price_gbp_mwh",
                    transformations=[
                        "explicit sample order-book row",
                        f"mapped {side} level {level} price",
                    ],
                    checks=[
                        ValidationCheck(
                            name="positive_price",
                            passed=math.isfinite(price) and price > 0,
                            detail="finite price > 0 GBP/MWh",
                        )
                    ],
                    **common,
                )
            )
            values.append(
                NormalisedValue(
                    metric=f"market_{side}_volume_l{level}",
                    value=volume,
                    unit="MWh",
                    raw_field_name="volume_mwh",
                    transformations=[
                        "explicit sample order-book row",
                        f"mapped {side} level {level} volume",
                    ],
                    checks=[
                        ValidationCheck(
                            name="positive_volume",
                            passed=math.isfinite(volume) and volume > 0,
                            detail="finite level volume > 0 MWh",
                        )
                    ],
                    **common,
                )
            )
        return values


class ExplicitSyntheticAdapter(FeedAdapter):
    feed_id = "synthetic_demo"
    feed_name = "Synthetic diagnostic feed"
    description = "Generated diagnostic values, loaded only by an explicit manual refresh."
    source_mode = SourceMode.SYNTHETIC
    semantic_kind = SemanticKind.ESTIMATE
    cadence_seconds = 0
    freshness_sla_seconds = 3600
    include_by_default = False

    async def fetch(self, now: datetime) -> RawFeedResult:
        rng = random.Random(now.replace(second=0, microsecond=0).isoformat())
        return RawFeedResult(rows=[{"diagnostic_price_gbp_mwh": round(rng.uniform(45, 95), 2)}], retrieved_at=now)

    def normalise(self, result: RawFeedResult) -> list[NormalisedValue]:
        value = float(result.rows[0]["diagnostic_price_gbp_mwh"])
        return [
            NormalisedValue(
                metric="synthetic_diagnostic_price",
                value=value,
                unit="GBP/MWh",
                raw_field_name="diagnostic_price_gbp_mwh",
                published_at=result.retrieved_at,
                transformations=["explicit seeded random diagnostic generation"],
                checks=[ValidationCheck(name="finite", passed=True, detail="finite diagnostic number")],
                warnings=["Synthetic diagnostic only; excluded from optimisation readiness."],
            )
        ]


def adapters() -> list[FeedAdapter]:
    return [
        ElexonSystemAdapter(),
        NesoSystemAdapter(),
        SampleForecastAdapter(),
        SamplePositionAdapter(),
        SampleBatteryAdapter(),
        SampleBatteryConfigAdapter(),
        UnconfiguredMarketAdapter(),
        SampleMarketOrderBookAdapter(),
        SampleServiceAdapter(),
        ExplicitSyntheticAdapter(),
    ]


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)
