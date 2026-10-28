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

from greensettle.settlement.rules import SettlementParams, SettlementResult

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
    net: int  # always exactly fee - rebate + surcharge


def settlement_to_micro(result: SettlementResult, params: SettlementParams) -> MicroSettlement:
    """Round a SettlementResult to integer micro-credits, keeping the guarantees.

    1. fee and surcharge are rounded to the nearest micro-credit.
    2. The rebate is rounded too, but then limited to floor(gamma x fee_micro).
       Rounding alone could push the rebate 1 micro-credit ABOVE the cap
       (e.g. when the cap binds and gamma x fee is not a whole number), which
       would break the "rebate <= gamma x fee" promise. floor() gives the
       largest whole number that still respects it.
    3. net is computed from the ROUNDED parts, so fee - rebate + surcharge
       adds up exactly in the ledger.
    """
    fee = to_micro(result.fee)
    cap = math.floor(params.rebate_cap * fee)
    rebate = max(0, min(to_micro(result.rebate), cap))
    surcharge = to_micro(result.surcharge)
    return MicroSettlement(fee=fee, rebate=rebate, surcharge=surcharge, net=fee - rebate + surcharge)
