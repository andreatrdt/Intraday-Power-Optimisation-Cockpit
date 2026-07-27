# Trader Decision Queue (frontend, Milestone 5)

The `/decisions` page ([`DecisionQueuePage.tsx`](../frontend/src/DecisionQueuePage.tsx))
is the first genuinely trader-facing screen: a ranked, concise **decision queue**
built on the existing backend workflow. It is decision-first, not chart-first, so
a trader can read within ~10 seconds *what changed, which periods matter, what is
suggested, why, and what data is missing / SAMPLE / non-executable*.

It **executes no trades**. Every action and badge is diagnostic-only and
non-executable. All decision / revision / timing / priority logic stays on the
backend — the client only reads, joins linked records by id, filters and sorts.

## Backend endpoints consumed

Read: `GET /decisions`, `/decisions/{id}`, `/decision-batches`,
`/forecast-revisions`, `/forecast-revisions/{id}`, `/hedge-timing-assessments`,
`/decision-batch-summaries`. Actions (POST, then reload): `/decisions/refresh`
(create new material decisions), `/decisions/assess-timing` (rank timing).
Lists are fetched once and joined client-side by id (`forecast_revision_id`,
`decision_id`).

## Page structure

* **A. Header** — title *Trader Decision Queue*, run mode / source / quality /
  connection / last-refresh, and `SAMPLE` · `DIAGNOSTIC ONLY` · `NOT EXECUTABLE`
  (· `DEGRADED` when calculation is not allowed) badges. `Refresh decisions` and
  `Reassess timing` call the backend then reload; a note states neither submits a
  trade.
* **B. Summary strip** — active decisions, critical, high, hedge-now, partial,
  wait, no-action, batches, unassessed.
* **C. Ranked queue** — only the prioritised **top 8** (mirrors the backend's
  deterministic `prioritise()` order and default `top_n_cap`). Each card shows
  priority, verdict (`HEDGE NOW` / `PARTIAL HEDGE` / `WAIT` / `NO ACTION`), SP,
  minutes-to-gate, market direction (BUY/SELL/NO ACTION), total / now / deferred
  volume, latest P50 exposure, P10/P90 range, ΔP50 revision, revision
  significance (only when available), spread, executable depth, executable WAP,
  the primary reason, a warnings count, delivery window, and source/quality
  badges. Visual hierarchy runs verdict+priority → action+volume → gate →
  exposure+revision → liquidity → reasons.
* **D. Detail drawer** (click a card) — *What changed* (prev/latest P10/P50/P90,
  ΔP10/ΔP50/ΔP90, uncertainty-width change, horizon, materiality reasons);
  *Portfolio impact* (Q, previous & latest P10/P50/P90 exposure, direction
  before→after, zero-crossing) with the canonical sign convention shown;
  *Timing assessment* (verdict, priority, now/deferred, urgency / liquidity /
  exposure-risk / gate-closure / revision-significance components, spread/slippage
  penalty, reasons, warnings); *Recommendation provenance* (revision id, vintage
  ids, market snapshot id, optimisation run id, source/quality/run-mode, calc
  allowed, trustworthy-for-live-trading, diagnostic-only, not-executable);
  *Lifecycle* (current status + transition history, **read-only** — no mutation
  controls in this milestone).
* **E. Batch summaries** — per batch: affected period range, total, counts by
  priority and verdict, the capped top decision ids, and an expandable list of
  **all** periods. Kept separate from the ranked queue; every decision stays
  accessible.
* **G. Filters** — priority, verdict, action, quality, batch, settlement-period
  range. Default ordering is the backend priority ranking.

## Default ranking

Assessed decisions are sorted by `(priority band, priority_score desc,
gate_closure_score desc, decision_id)` — the same key the backend
`decision_prioritisation.prioritise()` uses — then capped at `TOP_CAP = 8`,
mirroring the backend default. All decisions remain stored and reachable via the
batch section; the cap is presentation-only.

## Trust badges

`SAMPLE` / `DIAGNOSTIC ONLY` / `NOT EXECUTABLE` always show; `DEGRADED` shows when
`calculation_allowed` is false. Per-card `Badge` shows source mode and quality.
The drawer surfaces `trustworthy_for_live_trading`, `calculation_allowed`,
`diagnostic_only` and `not_executable` verbatim. Revision significance is labelled
as **significance**, never "forecast confidence".

## Empty & degraded states

* **No decisions, no revisions** → explains that ≥2 complete forecast vintages
  are required, and to refresh the rolling environment then *Refresh decisions*.
* **Revisions but no decisions** → "Forecast revisions were calculated, but none
  crossed the materiality thresholds."
* **No timing yet** → prompts *Reassess timing*.
* **Backend unavailable** → error banner with Retry; the page does not crash.
* **Partial linked data** → the decision stays visible and shows *Supporting
  evidence unavailable*; missing fields are never fabricated.

## Limitations

* Read-only workflow — no accept / modify / reject / submit (a later milestone),
  no execution simulation, replay, benchmark P&L, or objective/CVaR changes.
* The ranked-queue cap is a client mirror of the backend default; the backend
  remains the authority (also exposed via each `DecisionBatchSummary.top_decision_ids`).
* Populating the SAMPLE queue requires a material forecast change (e.g. reset the
  rolling environment then switch to a wind-miss regime) followed by
  *Refresh decisions* and *Reassess timing*.
