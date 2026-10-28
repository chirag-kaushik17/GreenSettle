"""The server's simulated clock.

Real grid curves change over hours, but a demo lasts minutes. The server
therefore runs on SIMULATED time that moves `scale` times faster than real
time:

    sim_now = anchor_sim + (real_now - anchor_real) * scale

  anchor_real  a real moment (epoch seconds, from real_time())
  anchor_sim   the simulated datetime at that moment
  scale        sim seconds per real second (--time-scale, default 1)

Changing the scale "re-anchors": we compute the current sim time with the old
scale and make that the new anchor, so simulated time never jumps.

What uses which time:
  SIM time  (aware UTC datetimes): scheduling, planned starts, ledger timestamps
  REAL time (epoch seconds):       leases, worker start/end times, telemetry

real_time is injected (default time.time) so tests can control it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

RealTimeFn = Callable[[], float]


class Clock:
    def __init__(
        self,
        anchor_sim: datetime,
        anchor_real: float,
        scale: float = 1.0,
        real_time: RealTimeFn = time.time,
    ) -> None:
        if anchor_sim.tzinfo is None:
            raise ValueError("anchor_sim must be timezone-aware")
        _check_scale(scale)
        self.anchor_sim = anchor_sim.astimezone(timezone.utc)
        self.anchor_real = anchor_real
        self.scale = scale
        self.real_time = real_time

    @classmethod
    def start(cls, sim_start: datetime | None = None, scale: float = 1.0,
              real_time: RealTimeFn = time.time) -> "Clock":
        """A brand-new clock. By default sim time starts at the current real UTC time."""
        real_now = real_time()
        anchor_sim = sim_start or datetime.fromtimestamp(real_now, timezone.utc)
        return cls(anchor_sim, real_now, scale, real_time)

    @classmethod
    def resume(cls, anchor_sim: datetime, anchor_real: float, old_scale: float,
               new_scale: float, real_time: RealTimeFn = time.time) -> "Clock":
        """Continue a clock saved before a restart.

        Sim time is taken to have kept running at the OLD scale while the
        server was down (continuity), then we re-anchor there with the new scale.
        """
        old = cls(anchor_sim, anchor_real, old_scale, real_time)
        return cls(old.now(), real_time(), new_scale, real_time)

    def real_to_sim(self, real_epoch: float) -> datetime:
        return self.anchor_sim + timedelta(seconds=(real_epoch - self.anchor_real) * self.scale)

    def now(self) -> datetime:
        return self.real_to_sim(self.real_time())

    def sim_seconds(self, real_seconds: float) -> float:
        """A real duration (e.g. the user's est_duration_s) expressed in sim seconds."""
        return real_seconds * self.scale

    def set_scale(self, new_scale: float) -> None:
        """Change speed without making simulated time jump."""
        _check_scale(new_scale)
        real_now = self.real_time()
        self.anchor_sim = self.real_to_sim(real_now)
        self.anchor_real = real_now
        self.scale = new_scale


def _check_scale(scale: float) -> None:
    if not scale > 0:
        raise ValueError("time scale must be > 0")
