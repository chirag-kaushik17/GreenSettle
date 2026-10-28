"""Every Phase 2 number in ONE place.

ALL VALUES ARE PLACEHOLDERS. They are reasonable starting points for a demo,
not measured or tuned values, and must be justified or calibrated later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from greensettle.settlement.rules import SettlementParams


@dataclass(frozen=True)
class TierInfo:
    name: str
    min_balance_credits: float  # lowest balance (credits, after decay) that is in this tier
    priority: int  # higher goes first in the queue
    max_concurrent: int  # running jobs allowed per user


DEFAULT_TIERS: tuple[TierInfo, ...] = (
    TierInfo("red", -math.inf, priority=0, max_concurrent=1),  # < 20, including debt
    TierInfo("standard", 20.0, priority=1, max_concurrent=1),  # 20 <= b < 100
    TierInfo("green", 100.0, priority=2, max_concurrent=2),  # b >= 100
)


@dataclass(frozen=True)
class LedgerConfig:
    half_life_days: float = 30.0  # PLACEHOLDER: a positive balance halves every 30 days
    allowance_credits: float = 100.0  # PLACEHOLDER: granted per calendar month ("YYYY-MM")
    tiers: tuple[TierInfo, ...] = DEFAULT_TIERS  # PLACEHOLDER thresholds, priorities, limits
    settlement: SettlementParams = field(default_factory=SettlementParams)  # PLACEHOLDER p, rho, gamma, k, sigma


DEFAULT_CONFIG = LedgerConfig()


def tier_for(balance_credits: float, tiers: tuple[TierInfo, ...] = DEFAULT_TIERS) -> TierInfo:
    """The tier for a balance (credits, AFTER decay). Pure function.

    The tier with the highest min_balance_credits that the balance reaches:
    exactly 20 is standard, exactly 100 is green, anything below 20
    (including a negative balance, i.e. debt) is red.
    """
    reached = [t for t in tiers if balance_credits >= t.min_balance_credits]
    if not reached:
        raise ValueError("the tier table must have a lowest tier with min_balance_credits = -inf")
    return max(reached, key=lambda t: t.min_balance_credits)
