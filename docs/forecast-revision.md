# Forecast revision service

The forecast revision service compares the latest forecast vintage with the
immediately preceding valid vintage **for one settlement period at a time** and
produces an auditable, frozen `ForecastRevision`. A material revision is a
*trigger candidate* that a later milestone can turn into a single-period
`TradeDecision` — this module does **not** create or transition decisions.

It is additive: it changes no existing module, route or optimiser output.

## Module layout

The service is split across three modules with a strict dependency direction
(models ← calibration ← service; no cycles):

| Module | Contents |
|---|---|
| [`forecast_revision_models.py`](../backend/src/cockpit/forecast_revision_models.py) | frozen base, enums, errors, `VintageForecastPoint`, `MaterialityConfig`, and all result records (`ForecastComparison`, `PortfolioEffect`, `RevisionSignificance`, `MaterialityAssessment`, `ForecastRevision`, `ForecastRevisionBatch`, `SkippedPeriod`, `ForecastRevisionRun`) |
| [`forecast_calibration.py`](../backend/src/cockpit/forecast_calibration.py) | `horizon_bucket`, `CalibrationResult`, `ResidualSample`, `ForecastErrorCalibration` and its providers |
| [`forecast_revision.py`](../backend/src/cockpit/forecast_revision.py) | selection & point-in-time guards, pure calculations, significance & materiality assessment, `ForecastRevisionService`, the `SERVICE` singleton — and re-exports of the public surface (`__all__`) so callers can `from cockpit.forecast_revision import …` |

## Sign conventions

Shared with `position_layer` and the rest of the cockpit — the canonical form is:

```
I_t^s = G_t^s − Q_t          [MWh]

where  G_t^s = forecast generation for quantile/scenario s
       Q_t   = contracted position

I_t^s > +tol  →  LONG   (more generation than sold)
I_t^s < −tol  →  SHORT  (sold more than generated)
|I_t^s| ≤ tol →  FLAT           (tol = flat_tolerance_mwh, default 0.05)
```

Because Q_t is unchanged between two vintages of the same period, the **P50
exposure change equals the P50 generation revision** (`delta_p50_exposure_mwh ==
delta_p50_mwh`).

Revisions are signed *latest − previous*: a downward forecast revision is
negative.

## What a forecast vintage is

A vintage is one issuance of a forecast model, identified by `vintage_id` and
stamped with a `published_at` time. The service consumes `VintageForecastPoint`
inputs — one vintage's P10/P50/P90 for one settlement period — deliberately free
of market/portfolio concepts (Q and Gate Closure are passed separately).

## Predecessor-selection rules

1. **Point-in-time:** only points with `published_at ≤ as_of` are eligible.
   Future vintages are excluded (not an error). A programmatic look-ahead guard
   (`_assert_not_future`) additionally rejects any *selected* point that is in
   the future.
2. **Latest** = the chronologically most recent eligible point (tie-broken by
   `vintage_id`). It must be valid — a malformed latest vintage is rejected, not
   skipped.
3. **Previous** = the most recent eligible point of a *different* `vintage_id`
   before the latest; it must itself be valid.
4. If there is no eligible latest → `MissingLatestVintageError`; if no valid
   different predecessor → `MissingPreviousVintageError`.

Reject conditions (never silently repaired): publication after `as_of`
(`LookAheadError`), settlement-period mismatch (`SettlementPeriodMismatchError`),
unit mismatch (`UnitMismatchError`), non-monotone P10≤P50≤P90
(`InvalidQuantileOrderError`). `compute_revision` raises; `compute_run` captures
these per period into `skipped` and continues.

## Revision formulae

For quantile q ∈ {P10, P50, P90}:

```
delta_q                    = latest_q − previous_q
uncertainty_width          = p90 − p10                       (per vintage)
uncertainty_width_change   = latest_width − previous_width
absolute_revision_mwh      = |delta_p50|
percentage_revision        = delta_p50 / previous_p50,
                             null when |previous_p50| < min_percentage_denominator_mwh (default 1.0)
forecast_horizon_minutes   = (delivery_start − as_of) / 60

exposure_q(vintage)        = q − Q
delta_p50_exposure_mwh     = latest_p50_exposure − previous_p50_exposure
crossed_zero_exposure      = strict sign flip of P50 exposure beyond the flat tolerance
```

## Revision significance & calibration basis

The `RevisionSignificance` record standardises the P50 revision against a
forecast-error dispersion. When a positive std is available:

```
revision_z_score            = delta_p50 / error_std_mwh
revision_significance_score = erf(|revision_z_score| / √2)
                            = P(|N(0,1)| ≤ |z|)   ∈ [0, 1)
```

**What this measures — and what it does not.** These are honest names on purpose:

* `revision_z_score` standardises a **forecast-to-forecast revision** (`latest −
  previous`) using an **error-dispersion proxy** — the std of forecast *errors*
  (`actual − forecast`). Forecast-error standard deviation and
  forecast-*revision* standard deviation are **not generally identical**; the
  error std is used as a pragmatic proxy and should be read as such.
* `revision_significance_score` measures the **statistical unusualness** of the
  revision (how far it sits from zero in units of that dispersion). It is **not**
  a measure of the **reliability of the latest forecast**: a large, confidently
  wrong revision scores high.
* A genuine forecast-reliability/confidence score would require
  **forecast-versus-actual** analysis — interval calibration and coverage (do
  P10–P90 bands contain the outcome ≈80% of the time?), bias, sharpness, and
  skill versus a baseline (persistence/climatology), ideally conditioned on
  horizon and regime. That is a different quantity and needs data this repository
  does not yet contain.

`calibration_basis` (`CalibrationBasis`) states the std's provenance honestly:

| Basis | Meaning | Ships in repo |
|---|---|---|
| `CALIBRATED` | genuine historical forecast-error residuals | **No** — no real history is bundled |
| `SAMPLE_DERIVED` | std from SAMPLE simulated residuals (demo only) | `SampleDerivedCalibration` |
| `ASSUMPTION_BASED` | an explicit, caller-supplied assumed std | `AssumptionCalibration` |
| `UNAVAILABLE` | no usable statistic; `revision_z_score` and `revision_significance_score` are `null` | `UnavailableCalibration` (default) |

The default provider is `UnavailableCalibration`, so out of the box the z-score
and significance are `null` and the basis is `UNAVAILABLE`. A hard-coded standard
deviation is **never** substituted silently — an assumed std must be passed
explicitly via `AssumptionCalibration` and is labelled `ASSUMPTION_BASED`.
SAMPLE-derived statistics are never labelled `CALIBRATED`.

## Materiality logic

Materiality is decomposed into transparent, configurable components
(`MaterialityConfig`), not one opaque score:

| Component | True when |
|---|---|
| `absolute_volume_material` | `|delta_p50| ≥ absolute_mwh_threshold` **or** `|percentage_revision| ≥ percentage_threshold` |
| `standardised_revision_material` | `z` available and `|z| ≥ z_score_threshold` |
| `exposure_change_material` | `|delta_p50_exposure| ≥ p50_exposure_change_threshold_mwh` |
| `direction_flip_material` | `direction_flip_is_material` and exposure crossed zero |
| `gate_closure_material` | `0 ≤ minutes_to_gate_closure ≤ gate_closure_minutes_threshold` |

`is_material` is the OR of the four **signal** components. Gate closure is
reported and amplifies the `signal_materiality_score` (and appears in
`materiality_reasons`) but is **not sufficient on its own** — proximity to Gate
Closure with no forecast-revision signal is not a revision trigger.

`signal_materiality_score` is a transparent additive score (≈1.0 per component at
its threshold, plus a weighted gate-closure urgency term). It is a **signal /
operational** materiality measure, **not an economic (£) one**: it contains no
price, spread, depth, imbalance cost or expected execution impact. Genuine
economic materiality in £ arrives with the hedge-timing and execution layers
(Phases 6/12) as a separate quantity, not by overloading this score.
`materiality_reasons` lists the specific thresholds crossed. A trigger candidate
is a revision with `is_material == True`; the service produces candidates but
never creates or transitions a `TradeDecision`.

## Models & batching

Every model is a frozen Pydantic model with tuple collections. A
`ForecastRevision` is single-period and composes `ForecastComparison`,
`PortfolioEffect`, `RevisionSignificance` and `MaterialityAssessment`. A
`ForecastRevisionBatch` is a lightweight index over the revision IDs from one
vintage update (IDs + affected periods only — no aggregate exposure or P&L).
`compute_run` returns a `ForecastRevisionRun` with the revisions, the batch,
`trigger_candidate_ids`, and any `skipped` periods.

## Limitations of SAMPLE-derived calibration

`SampleDerivedCalibration` buckets residuals by horizon and returns a per-bucket
std **only** when a bucket has at least `min_samples` observations with non-zero
dispersion; otherwise it reports `UNAVAILABLE`. Its output is explicitly
`SAMPLE_DERIVED` and must not be read as evidence about real forecast skill — it
exists for demonstration and tests. No synthetic history is generated merely to
produce a z-score.

## Scope (this milestone)

* No `TradeDecision` creation/transition (Milestone 3).
* No wiring into the rolling pipeline/API (Milestone 3/4).
* Stateless service operating on supplied `VintageForecastPoint` inputs; the
  rolling SAMPLE environment may later supply these and SAMPLE residuals for
  demonstration.
