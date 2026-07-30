"""Pure aggregate metrics for a completed replay run (Milestone 8).

No I/O, no engine, no storage — deterministic functions over the immutable
episode results. Every metric documents its numerator/denominator, how skipped
episodes and missing values are treated, and whether it is incremental-vs-no-action
or absolute cash flow. Decision counts and settlement-period counts are never mixed.

Statistical caution: these are descriptive diagnostics. No annualisation, Sharpe,
confidence intervals, p-values or significance claims are produced — a SAMPLE replay
is too small to justify them. ``sample_size`` accompanies every aggregate.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable

from cockpit.replay_models import (
    CoverageMetrics,
    CumulativePnlPoint,
    ExecutionMetrics,
    HitRegretMetrics,
    LifecyclePath,
    PnlMetrics,
    ReplayEpisodeResult,
    ReplayMetrics,
    ReplayMode,
    RiskMetrics,
    SegmentMetric,
    TimingMetrics,
)

IN_LINE_TOLERANCE_GBP = 0.01


def _evaluated(episodes: list[ReplayEpisodeResult]) -> list[ReplayEpisodeResult]:
    """Episodes that reached settlement/evaluation (have a realised incremental P&L).
    Skipped episodes are excluded from every P&L/risk/hit denominator."""
    return [e for e in episodes if e.skip_reason is None and e.realised_incremental_pnl_gbp is not None]


def _submitted(episodes: list[ReplayEpisodeResult]) -> list[ReplayEpisodeResult]:
    return [e for e in episodes if e.simulated_order_ids]


def _safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _pct(count: int, total: int) -> float | None:
    return 100.0 * count / total if total else None


def coverage_metrics(episodes: list[ReplayEpisodeResult]) -> CoverageMetrics:
    material = [e for e in episodes if e.decision_ids]
    submitted = _submitted(episodes)
    by_path: Callable[[LifecyclePath], int] = lambda p: sum(1 for e in episodes if e.lifecycle_path is p)
    return CoverageMetrics(
        total_eligible_periods=len(episodes),
        periods_with_valid_revisions=sum(1 for e in episodes if e.revision_ids),
        material_decision_count=len(material),
        submitted_decision_count=len(submitted),
        filled_count=by_path(LifecyclePath.FILLED),
        partial_filled_count=by_path(LifecyclePath.PARTIALLY_FILLED),
        expired_count=by_path(LifecyclePath.EXPIRED),
        evaluated_count=len(_evaluated(episodes)),
        skipped_count=sum(1 for e in episodes if e.skip_reason is not None),
        action_rate=_safe_div(len(submitted), len(material)),
    )


def pnl_metrics(episodes: list[ReplayEpisodeResult]) -> PnlMetrics:
    ev = _evaluated(episodes)
    pnls = [e.realised_incremental_pnl_gbp for e in ev if e.realised_incremental_pnl_gbp is not None]
    return PnlMetrics(
        sample_size=len(ev),
        total_incremental_pnl_gbp=round(sum(pnls), 4),
        mean_incremental_pnl_gbp=round(statistics.fmean(pnls), 4) if pnls else None,
        median_incremental_pnl_gbp=round(statistics.median(pnls), 4) if pnls else None,
        stdev_incremental_pnl_gbp=round(statistics.pstdev(pnls), 4) if len(pnls) >= 2 else None,
        min_incremental_pnl_gbp=round(min(pnls), 4) if pnls else None,
        max_incremental_pnl_gbp=round(max(pnls), 4) if pnls else None,
        total_realised_cashflow_gbp=round(sum(e.total_realised_cashflow_gbp or 0.0 for e in ev), 4),
        total_fees_gbp=round(sum(e.fees_gbp for e in ev), 4),
        total_slippage_gbp=round(sum(e.slippage_gbp for e in ev), 4),
    )


def hit_regret_metrics(episodes: list[ReplayEpisodeResult]) -> HitRegretMetrics:
    ev = _evaluated(episodes)
    n = len(ev)
    out = sum(1 for e in ev if (e.realised_incremental_pnl_gbp or 0.0) > IN_LINE_TOLERANCE_GBP)
    under = sum(1 for e in ev if (e.realised_incremental_pnl_gbp or 0.0) < -IN_LINE_TOLERANCE_GBP)
    in_line = n - out - under
    regrets_model = [e.regret_vs_model_gbp for e in ev if e.regret_vs_model_gbp is not None]
    regrets_pf = [e.regret_vs_perfect_foresight_gbp for e in ev if e.regret_vs_perfect_foresight_gbp is not None]
    trader_sum = sum(e.realised_incremental_pnl_gbp or 0.0 for e in ev)
    perfect_sum = sum(e.perfect_foresight_pnl_gbp or 0.0 for e in ev)
    # Capture ratio: trader incremental / perfect-foresight incremental. Perfect
    # foresight is the (non-negative) upper bound, so a zero or negative denominator
    # is not meaningful — report None with a note rather than a misleading number.
    if perfect_sum <= 0:
        capture: float | None = None
        capture_note = "Perfect-foresight incremental P&L is zero/negative in this SAMPLE; capture ratio undefined."
    else:
        capture = round(trader_sum / perfect_sum, 4)
        capture_note = "trader incremental P&L / perfect-foresight incremental P&L (upper bound); hindsight, not attainable."
    return HitRegretMetrics(
        sample_size=n,
        pct_outperforming_no_action=_pct(out, n),
        pct_underperforming_no_action=_pct(under, n),
        pct_in_line=_pct(in_line, n),
        mean_regret_vs_model_gbp=round(statistics.fmean(regrets_model), 4) if regrets_model else None,
        mean_regret_vs_perfect_foresight_gbp=round(statistics.fmean(regrets_pf), 4) if regrets_pf else None,
        perfect_foresight_capture_ratio=capture,
        capture_ratio_note=capture_note,
    )


def risk_metrics(episodes: list[ReplayEpisodeResult]) -> RiskMetrics:
    ev = _evaluated(episodes)
    pnls = [e.realised_incremental_pnl_gbp or 0.0 for e in ev]
    n = len(pnls)
    if n == 0:
        return RiskMetrics(sample_size=0, max_drawdown_gbp=None, worst_single_period_loss_gbp=None,
                           downside_deviation_gbp=None, fifth_percentile_gbp=None, loss_frequency=None)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    downside = [min(0.0, pnl) for pnl in pnls]
    downside_dev = (statistics.fmean([d * d for d in downside])) ** 0.5
    losses = sum(1 for pnl in pnls if pnl < 0)
    return RiskMetrics(
        sample_size=n,
        max_drawdown_gbp=round(max_dd, 4),
        worst_single_period_loss_gbp=round(min(pnls), 4),
        downside_deviation_gbp=round(downside_dev, 4),
        fifth_percentile_gbp=round(_percentile(pnls, 5.0), 4),
        loss_frequency=_pct(losses, n),
    )


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile on a small sample (descriptive only)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    frac = rank - low
    if low + 1 >= len(ordered):
        return ordered[low]
    return ordered[low] + frac * (ordered[low + 1] - ordered[low])


def execution_metrics(episodes: list[ReplayEpisodeResult]) -> ExecutionMetrics:
    submitted = _submitted(episodes)
    n = len(submitted)
    filled = sum(1 for e in submitted if e.lifecycle_path is LifecyclePath.FILLED)
    partial = sum(1 for e in submitted if e.lifecycle_path is LifecyclePath.PARTIALLY_FILLED)
    priced = [e for e in submitted if e.average_execution_price_gbp_per_mwh is not None and (e.executed_buy_mwh + e.executed_sell_mwh) > 0]
    volume = sum(e.executed_buy_mwh + e.executed_sell_mwh for e in priced)
    notional = sum((e.average_execution_price_gbp_per_mwh or 0.0) * (e.executed_buy_mwh + e.executed_sell_mwh) for e in priced)
    return ExecutionMetrics(
        submitted_count=n,
        fill_rate=_safe_div(filled, n),
        partial_fill_rate=_safe_div(partial, n),
        average_slippage_gbp=round(statistics.fmean([e.slippage_gbp for e in submitted]), 4) if submitted else None,
        average_fee_gbp=round(statistics.fmean([e.fees_gbp for e in submitted]), 4) if submitted else None,
        average_levels_consumed=round(statistics.fmean([e.levels_consumed for e in submitted]), 4) if submitted else None,
        volume_weighted_execution_price_gbp_per_mwh=round(notional / volume, 4) if volume else None,
    )


def _segment(episodes: list[ReplayEpisodeResult], dimension: str, key: Callable[[ReplayEpisodeResult], str | None]) -> list[SegmentMetric]:
    buckets: dict[str, list[ReplayEpisodeResult]] = {}
    for episode in episodes:
        segment = key(episode)
        if segment is None:
            continue
        buckets.setdefault(segment, []).append(episode)
    out: list[SegmentMetric] = []
    for segment in sorted(buckets):
        group = buckets[segment]
        ev = _evaluated(group)
        pnls = [e.realised_incremental_pnl_gbp or 0.0 for e in ev]
        out.append(SegmentMetric(
            dimension=dimension,
            segment=segment,
            episode_count=len(group),
            evaluated_count=len(ev),
            total_incremental_pnl_gbp=round(sum(pnls), 4),
            mean_incremental_pnl_gbp=round(statistics.fmean(pnls), 4) if pnls else None,
        ))
    return out


def _horizon_bucket(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    if minutes < 60:
        return "0-60m"
    if minutes < 120:
        return "60-120m"
    if minutes < 240:
        return "120-240m"
    return "240m+"


def timing_metrics(episodes: list[ReplayEpisodeResult]) -> TimingMetrics:
    count = lambda verdict: sum(1 for e in episodes if e.timing_verdict == verdict)
    return TimingMetrics(
        hedge_now_count=count("HEDGE_NOW"),
        partial_hedge_now_count=count("PARTIAL_HEDGE_NOW"),
        wait_count=count("WAIT"),
        no_action_count=count("NO_ACTION"),
        mean_incremental_pnl_by_verdict=tuple(_segment(episodes, "timing_verdict", lambda e: e.timing_verdict)),
        mean_incremental_pnl_by_priority=tuple(_segment(episodes, "timing_priority", lambda e: e.timing_priority)),
    )


def segmentation_metrics(episodes: list[ReplayEpisodeResult]) -> list[SegmentMetric]:
    """Typed breakdowns exposed for the API/UI (not all shown in the first UI)."""
    return [
        *_segment(episodes, "forecast_horizon", lambda e: _horizon_bucket(e.forecast_horizon_minutes)),
        *_segment(episodes, "settlement_period", lambda e: f"SP{e.settlement_period}"),
        *_segment(episodes, "timing_verdict", lambda e: e.timing_verdict),
        *_segment(episodes, "timing_priority", lambda e: e.timing_priority),
        *_segment(episodes, "recommended_action", lambda e: e.recommended_action),
    ]


def cumulative_pnl_series(episodes: list[ReplayEpisodeResult]) -> list[CumulativePnlPoint]:
    """Ordered cumulative incremental-P&L series for trader / model / no-action /
    perfect-foresight (no-action is 0 by construction; perfect foresight is the
    hindsight upper bound, kept as a separate series)."""
    ev = sorted(_evaluated(episodes), key=lambda e: (e.settlement_period, e.episode_id))
    points: list[CumulativePnlPoint] = []
    trader = model = no_action = perfect = 0.0
    for index, episode in enumerate(ev):
        trader += episode.realised_incremental_pnl_gbp or 0.0
        model += episode.model_pnl_gbp or 0.0
        perfect += episode.perfect_foresight_pnl_gbp or 0.0
        points.append(CumulativePnlPoint(
            index=index,
            settlement_period=episode.settlement_period,
            episode_id=episode.episode_id,
            incremental_pnl_gbp=round(episode.realised_incremental_pnl_gbp or 0.0, 4),
            cumulative_trader_gbp=round(trader, 4),
            cumulative_model_gbp=round(model, 4),
            cumulative_no_action_gbp=round(no_action, 4),
            cumulative_perfect_foresight_gbp=round(perfect, 4),
        ))
    return points


def build_metrics(replay_run_id: str, run_mode: ReplayMode, episodes: list[ReplayEpisodeResult]) -> ReplayMetrics:
    ev = _evaluated(episodes)
    note = (
        f"{len(ev)} evaluated SAMPLE episodes — descriptive diagnostics only; "
        "too small for annualised or significance claims."
        if run_mode is ReplayMode.SAMPLE_REPLAY
        else f"{len(ev)} evaluated episodes."
    )
    return ReplayMetrics(
        replay_run_id=replay_run_id,
        run_mode=run_mode,
        sample_size=len(ev),
        sample_size_note=note,
        coverage=coverage_metrics(episodes),
        pnl=pnl_metrics(episodes),
        hit_regret=hit_regret_metrics(episodes),
        risk=risk_metrics(episodes),
        execution=execution_metrics(episodes),
        timing=timing_metrics(episodes),
        segments=tuple(segmentation_metrics(episodes)),
    )
