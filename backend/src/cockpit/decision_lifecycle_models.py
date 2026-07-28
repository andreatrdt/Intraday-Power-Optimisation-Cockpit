"""Request contracts for the trader-lifecycle mutation API (Milestone 6A).

These are thin input DTOs only. Stateless field validation lives here (types,
non-negative volumes, not-both-positive, finite limit price, non-blank required
rationale, timezone-aware delay time). Stateful lifecycle rules (legal
transitions, meaningful-change, delay-vs-Gate-Closure, optimistic concurrency)
stay in the decision service / state machine — never duplicated here or in the
API layer.

Every mutation carries optional optimistic-concurrency guards
(``expected_status`` / ``expected_sequence``); see ``docs/trader-lifecycle.md``.
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from cockpit.decision_models import DecisionStatus

_ACTION_TOLERANCE_MWH = 1e-6


def _non_blank(value: str) -> str:
    if value is None or not value.strip():
        raise ValueError("trader_rationale must not be blank")
    return value.strip()


class _LifecycleRequest(BaseModel):
    # Identity is optional: the project has no authentication.
    actor_id: str | None = None
    # Optimistic concurrency: at least one is recommended so stale writes 409.
    expected_status: DecisionStatus | None = None
    expected_sequence: int | None = None


class AcceptRequest(_LifecycleRequest):
    trader_rationale: str | None = None


class ModifyRequest(_LifecycleRequest):
    trader_buy_mwh: float = Field(ge=0)
    trader_sell_mwh: float = Field(ge=0)
    trader_limit_price: float | None = None
    trader_rationale: str

    @field_validator("trader_rationale")
    @classmethod
    def _rationale_required(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("trader_buy_mwh", "trader_sell_mwh")
    @classmethod
    def _finite_volume(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("volumes must be finite")
        return value

    @field_validator("trader_limit_price")
    @classmethod
    def _finite_limit(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("trader_limit_price must be finite when supplied")
        return value

    @model_validator(mode="after")
    def _not_both_positive(self) -> "ModifyRequest":
        if self.trader_buy_mwh > _ACTION_TOLERANCE_MWH and self.trader_sell_mwh > _ACTION_TOLERANCE_MWH:
            raise ValueError("buy and sell cannot both be positive (market hedge is one-sided)")
        return self


class RejectRequest(_LifecycleRequest):
    trader_rationale: str

    @field_validator("trader_rationale")
    @classmethod
    def _rationale_required(cls, value: str) -> str:
        return _non_blank(value)


class DelayRequest(_LifecycleRequest):
    delayed_until: datetime
    trader_rationale: str

    @field_validator("trader_rationale")
    @classmethod
    def _rationale_required(cls, value: str) -> str:
        return _non_blank(value)

    @field_validator("delayed_until")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("delayed_until must be timezone-aware")
        return value


class ReopenRequest(_LifecycleRequest):
    trader_rationale: str | None = None
