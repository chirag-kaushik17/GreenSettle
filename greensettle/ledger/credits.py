"""Converting credits (floats, from the settlement rule) to integer micro-credits.

The ledger stores every amount as an INTEGER number of micro-credits
(1 credit = 1,000,000 micro-credits). Adding up floats again and again
slowly drifts (0.1 + 0.2 != 0.3); adding integers never does. The
conversion happens once, here, at the boundary between the rule and the
ledger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from greensettle.settlement.rules import Settlement, SettlementParams

MICRO_PER_CREDIT = 1_000_000


def to_micro(credits: float) -> int:
    """Credits -> micro-credits, rounded to the nearest integer."""
    if not math.isfinite(credits):
        raise ValueError(f"cannot convert {credits!r} credits to micro-credits")
    return round(credits * MICRO_PER_CREDIT)


def from_micro(micro: int) -> float:
    """Micro-credits -> credits (for display only; never store the result)."""
    return micro / MICRO_PER_CREDIT


@dataclass(frozen=True)
class MicroSettlement:
    fee: int
    rebate: int
    surcharge: int
    net: int  # always exactly fee + surcharge - rebate


def settlement_to_micro(result: Settlement, params: SettlementParams) -> MicroSettlement:
    """Round a Settlement to integer micro-credits so rounding NEVER favours the user.

    1. fee and surcharge round UP   (math.ceil): the user never pays less.
    2. rebate rounds DOWN           (math.floor): the user never gets more,
       and it is then capped at floor(gamma x fee_micro), so the promise
       "rebate <= gamma x fee" still holds after rounding.
    3. net = fee + surcharge - rebate, computed from the ROUNDED parts, so
       the ledger rows add up exactly.

    Note: because the inputs are floats, a value such as 14.000000000000002
    credits rounds up to 14,000,001 micro-credits. That float noise can cost
    the user at most one micro-credit per row, always in the system's favour.
    """
    for value in (result.fee, result.rebate, result.surcharge):
        if not math.isfinite(value):
            raise ValueError(f"cannot convert {value!r} credits to micro-credits")
    fee = math.ceil(result.fee * MICRO_PER_CREDIT)
    surcharge = math.ceil(result.surcharge * MICRO_PER_CREDIT)
    cap = math.floor(params.gamma * fee)
    rebate = max(0, min(math.floor(result.rebate * MICRO_PER_CREDIT), cap))
    return MicroSettlement(fee=fee, rebate=rebate, surcharge=surcharge, net=fee + surcharge - rebate)
