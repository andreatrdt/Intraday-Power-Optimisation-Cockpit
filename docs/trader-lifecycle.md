# Trader lifecycle API & UI (Milestone 6A)

Lets a trader **record their decision** on a model recommendation while keeping
execution completely separate. Supports only `ACCEPT` / `MODIFY` / `REJECT` /
`DELAY` (and `REOPEN` from `DELAYED`). Nothing here submits an order, executes,
fills, cancels, expires, settles or computes P&L.

```
PROPOSED → ACCEPTED / MODIFIED / REJECTED / DELAYED
DELAYED  → PROPOSED (reopen) / REJECTED
```

Lifecycle rules stay in the decision state machine and service
([`decision_service.py`](../backend/src/cockpit/decision_service.py),
[`decision_state_machine.py`](../backend/src/cockpit/decision_state_machine.py)).
The API ([`api.py`](../backend/src/cockpit/api.py)) is a thin layer that calls
those methods and maps outcomes to HTTP codes; request DTOs live in
[`decision_lifecycle_models.py`](../backend/src/cockpit/decision_lifecycle_models.py).

## Mutation endpoints

| Method & path | Body | Effect |
|---|---|---|
| `POST /api/v1/decisions/{id}/accept` | `AcceptRequest` | record acceptance (preserves the recommendation as the trader instruction) |
| `POST /api/v1/decisions/{id}/modify` | `ModifyRequest` | record a market-only override |
| `POST /api/v1/decisions/{id}/reject` | `RejectRequest` | record rejection with a reason |
| `POST /api/v1/decisions/{id}/delay` | `DelayRequest` | delay to a time before Gate Closure |
| `POST /api/v1/decisions/{id}/reopen` | `ReopenRequest` | `DELAYED → PROPOSED` only |

All return `{ decision, diagnostic_only: true, trustworthy_for_live_trading: false }`.
There is **no** generic arbitrary-transition endpoint.

## Request validation

Stateless checks live in the request DTOs (→ **422**); stateful checks live in
the service (→ **422** `DecisionValidationError`):

* **Accept** — rationale optional; the model recommendation (buy/sell/limit) is
  copied into the `ACCEPT` trader instruction.
* **Modify** — `trader_buy_mwh`/`trader_sell_mwh` ≥ 0 and finite; not both
  positive (market hedge is one-sided; battery is never mapped into buy/sell);
  `trader_limit_price` finite when supplied; `trader_rationale` non-blank; and
  (service) the instruction must **differ** from the recommendation.
* **Reject** — `trader_rationale` non-blank.
* **Delay** — `delayed_until` timezone-aware, later than now, and **before Gate
  Closure**; a decision whose Gate Closure has passed cannot be delayed
  (`context.gate_closure_at` is the authority).
* **Reopen** — rationale optional; the state machine permits it only from
  `DELAYED`.

`404` for an unknown decision; `409` for an illegal transition (e.g. accepting an
already-accepted decision) or a stale write.

## Optimistic concurrency

Each request may carry `expected_status` and/or `expected_sequence`. Before
applying, the service (under a lock) checks them against the current decision; a
mismatch raises `StaleDecisionError` → **HTTP 409** with
`{ error: "stale_decision", current_status, current_sequence, message }` so the
client can reconcile.

Behaviour:
* two simultaneous accepts — only one wins; the second gets 409 (stale, or an
  invalid `ACCEPTED → ACCEPTED` transition);
* a modify based on an outdated `PROPOSED` version fails once the decision has
  changed;
* **retries are not idempotent** — re-sending an accepted request returns a
  clear 409 (the expected status/sequence no longer matches), rather than
  silently re-applying. The frontend always sends `expected_sequence`.

## Audit semantics

Every transition appends one immutable `DecisionTransition`
(`sequence`, `from_status`, `to_status`, `occurred_at`, `actor`, `reason`, and
optional `actor_id`). History is append-only and never mutated; there is **no**
mutable audit-metadata dictionary. `MODIFY`/`DELAY` also snapshot the trader
instruction (volumes / limit / delayed-until / rationale) on the decision.

## Trader instruction vs market execution

A trader instruction is a **recorded intent**, not an order. Even after `ACCEPTED`
or `MODIFIED`, the decision stays `trustworthy_for_live_trading = false`,
`diagnostic_only = true`, `not_executable = true`. Execution
(submit/fill/cancel/expire/settle) is a **separate, later** lifecycle stage and
is not part of this milestone.

## UI controls (`/decisions` detail drawer)

Controls are derived from the backend `status`:

* **PROPOSED** → *Accept recommendation*, *Modify*, *Reject*, *Delay*.
* **DELAYED** → *Reopen*, *Reject*.
* any other status → read-only, no controls.

Deliberate wording — *Record acceptance*, *Record modification*, *Record
rejection*, *Delay decision*, *Reopen decision* — and every form repeats *“This
records the trader decision only. No order will be submitted.”* No control uses
*Execute*, *Trade*, *Send order* or *Submit to market*.

Flows: **Accept** shows the recommendation + optional rationale; **Modify** shows
the model recommendation beside the editable trader instruction, highlights the
diff, and blocks both-positive/no-change; **Reject** requires a rationale and
shows the revision ΔP50 and timing verdict being rejected; **Delay** shows current
time, Gate Closure and the maximum allowable delay and blocks a time beyond Gate
Closure. On success the decision + queue reload (drawer stays open) with a concise
confirmation and the new status/transition; on failure the form values are kept,
the backend error is shown, and **409** is surfaced distinctly as a stale-decision
conflict with a *Reload* action. The queue shows a per-decision status tag and a
status filter (including *ACTIVE_ONLY*); rejected/delayed decisions are not
hidden by default.

## Limitations

* Records trader decisions only — no order submission, execution, fills,
  slippage, cancellation, expiry, settlement, realised P&L, benchmarks, replay,
  objective changes, CVaR or live routing.
* No authentication; `actor_id` is an optional free-text attribution.
* Concurrency guards are advisory — a client that omits `expected_*` still cannot
  perform an illegal transition (the state machine 409s), but only the guards
  detect a same-status change by another actor.
