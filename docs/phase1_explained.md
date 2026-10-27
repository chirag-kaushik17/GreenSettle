# GreenSettle Phase 1 ("Measure"), explained

GreenSettle's big idea is to run cloud jobs when the electricity is greener
and to settle carbon credits **after** each run from **measured** energy, not
from a guess made beforehand. Phase 1 builds only the measuring part:

```
LibreHardwareMonitor ──► lhm_client ──► Sampler ──► energy (J, kWh)
   (CPU Package W)        (parse)       (1 s polls)      │
                                                         ▼
                 psutil CPU time ─────────────► attribution (job's share)
                                                         │
                                                         ▼
                                              uncertainty (± kWh)
                                                         │
          intensity provider (g/kWh ± ) ───────► convert ─► gCO2e ± error
```

There is no scheduler, ledger, credit logic or dashboard yet. Those come in
later phases and will read the JSON files that `run` writes.

---

## 1. Where the watts come from

LibreHardwareMonitor (LHM) reads the CPU's built-in energy counters (Intel
RAPL) through its PawnIO driver and shows them as sensors. Its Remote Web
Server publishes every sensor as one JSON tree at
`http://127.0.0.1:8085/data.json`.

`agent/lhm_client.py`:

1. downloads that JSON (with a timeout),
2. walks the tree to the node whose `HardwareId` contains `cpu` (for example
   `/intelcpu/0`) and then to its child called `Powers`,
3. reads the four sensors **CPU Package**, **CPU Cores**, **CPU Memory** and
   **CPU Platform** by name.

For each sensor it prefers `RawValue`, a plain number. If that is `null` it
parses the display text `Value` instead (for example `"4.5 W"`, `"4,5 W"` on
a comma-decimal locale, or `"-"` for "no reading").

On this laptop only **CPU Package** and **CPU Cores** are live. **CPU
Memory** and **CPU Platform** always read 0.0 W, so `check-sensor` flags them
as DEAD. That is why every result says `"scope": "cpu_package"`: we measure
the CPU package only, not the RAM, screen, SSD or the rest of the laptop.

---

## 2. From watts to energy

**Power** (watts) is a rate and **energy** (joules) is an amount:
1 W for 1 s = 1 J. 1 kWh = 1000 W × 3600 s = 3,600,000 J.

The `Sampler` reads the CPU Package power about once a second on a background
thread. Each sample is stamped with `time.monotonic()`, a clock that never
jumps backwards (the normal wall clock can, for example when Windows syncs the
time).

### Trapezoidal rule, worked example

Suppose four samples arrive at the start of a job:

| t (s) | power (W) |
|------:|----------:|
| 0     | 4.6       |
| 1     | 26.0      |
| 2     | 28.4      |
| 3     | 28.0      |

Between two samples we assume power changed in a straight line, so each slice
is a trapezoid: *(average of the two readings) × (time gap)*.

- 0→1 s: (4.6 + 26.0) / 2 × 1 = **15.3 J**
- 1→2 s: (26.0 + 28.4) / 2 × 1 = **27.2 J**
- 2→3 s: (28.4 + 28.0) / 2 × 1 = **28.2 J**
- total **E_trapezoid = 70.7 J**

A cruder method, the **left Riemann sum**, assumes power stayed at the earlier
reading for the whole slice: 4.6 + 26.0 + 28.4 = **59.0 J**. The two methods
disagree by 11.7 J, almost all of it in the first second when power jumped.
That disagreement is our estimate of the **sampling error**: if power changes
a lot between polls, we cannot be sure what happened in between.

### Diagnostics

The sampler also records:

- `mean_sample_interval_s` and jitter: how regular the polls really were.
- The **unchanged fraction**: how often a poll returned exactly the same value
  as the previous one. LHM refreshes at its own rate (about 1 s), so polling
  faster than that only re-reads old values. A high unchanged fraction is a
  sign of that, not an error.

---

## 3. Idle calibration

`calibrate-idle` samples the CPU Package power for 30 s while the laptop does
nothing and saves the mean and standard deviation in `data/idle.json`. On
this laptop the GUI showed about 4.5 W at idle. If the standard deviation is
large compared to the mean, the machine was not really idle, so the command
prints a warning.

---

## 4. Which part of the energy belongs to the job?

The package energy measured during a job contains three things:

1. **Idle energy**: what the CPU would have used anyway, with or without the
   job.
2. **Dynamic energy caused by our job.**
3. **Dynamic energy caused by everything else** (the browser, antivirus, LHM
   itself, this agent).

Only (2) should be charged to the job. The formulas:

```
E_idle    = idle_power × duration
E_dynamic = max(0, E_pkg − E_idle)
cpu_share = clamp(job_cpu_seconds / system_busy_cpu_seconds, 0, 1)
E_job     = E_dynamic × cpu_share
```

`job_cpu_seconds` is the user + system CPU time of the job's process **and all
of its children**, counted with `psutil`. Children can finish before the job
does, so the tracker remembers the last value it saw for every process.
`system_busy_cpu_seconds` is how much CPU time the whole machine spent busy
(total minus idle) in the same window. Both are measured in CPU-seconds
summed over all cores, so 4 cores busy for 30 s is 120 CPU-seconds.

### Worked example: a 30 s, 4-worker burn job

| quantity | value |
|---|---|
| idle calibration | mean 4.5 W, std dev 0.4 W |
| duration | 30.0 s |
| E_pkg (trapezoid) | 810 J (average 27 W) |
| E_pkg (left Riemann) | 799.5 J |
| job CPU time | 112 CPU-s |
| system busy CPU time | 120 CPU-s |

1. E_idle = 4.5 W × 30 s = **135 J**
2. E_dynamic = 810 − 135 = **675 J**
3. cpu_share = 112 / 120 = **0.933**: our job did 93.3 % of all the CPU
   work in that window.
4. E_job = 675 × 0.933 = **630 J** = 630 / 3,600,000 = **0.000175 kWh**

### Known limitation: not all CPU-seconds cost the same

This Intel Core Ultra is a hybrid CPU with P-cores (fast, power-hungry),
E-cores, and low-power E-cores. One second on a P-core uses noticeably more
energy than one second on a low-power E-core. Splitting by CPU time treats
them as equal, so a job that ran mostly on P-cores is under-charged and one on
E-cores is over-charged. Phase 1 does not correct this. It is covered (for
now) by the `attribution_base_fraction` uncertainty term below.

### Optional platform overhead

Because the Memory and Platform sensors are dead, the rest of the platform is
invisible. `run --platform-overhead-w X --platform-overhead-unc-w Y` adds an
estimated X watts × duration × cpu_share to the job, with its own uncertainty
term. The scope then changes to `cpu_package+platform_estimate`. It is off by
default because we have no measured value for X.

---

## 5. Uncertainty: how wrong could E_job be?

There are four independent error sources, each expressed as ± kWh:

| term | formula | worked example |
|---|---|---|
| sensor | `sensor_tolerance_fraction × E_pkg` = 0.05 × 810 | **40.5 J** |
| sampling | `|E_trapezoid − E_left_riemann|` = |810 − 799.5| | **10.5 J** |
| idle baseline | `idle_std × duration` = 0.4 × 30 | **12.0 J** |
| attribution | `(base + k × (1 − cpu_share)) × E_dynamic` = (0.05 + 0.20 × 0.067) × 675 | **42.75 J** |

They are combined **in quadrature** (square, add, square root), the standard
way to add independent errors. They partly cancel out, so the total is
smaller than the plain sum but never smaller than the biggest term:

```
combined = √(40.5² + 10.5² + 12.0² + 42.75²) = √3722.06 ≈ 61.0 J ≈ 0.0000169 kWh
```

So the job used **630 ± 61 J**, about ±10 %.

Two honest notes:

- **The coefficients are placeholders.** `sensor_tolerance_fraction = 0.05`,
  `attribution_base_fraction = 0.05`, `attribution_k = 0.20` and the
  idle-warning threshold all live in one dataclass, `MeterConfig`, in
  `agent/meter.py`. Each is marked PLACEHOLDER. They are reasonable guesses,
  not measured facts, and a later phase must replace them with calibration
  experiments (for example a wall-socket power meter, or repeated runs of the
  same job).
- **The band is deliberately conservative.** The sensor, sampling and idle
  terms describe errors in the whole package or dynamic energy and are not
  scaled down by `cpu_share`. For a job with a small share, this overstates
  its error. We chose to report "too wide" rather than "too narrow".

---

## 6. From energy to gCO2e

Carbon intensity is how many grams of CO2-equivalent the grid emits per kWh
generated. It changes through the day: lower when sun and wind are strong,
higher when fossil plants fill the gap.

Phase 1 uses `SimulatedCurve`, a **made-up** but plausible daily curve, so
nothing depends on an API key:

- a base of 450 g/kWh,
- minus a bell-shaped "solar dip" centred on 13:00 local time (up to 220 g),
- plus three slow sine waves standing in for wind (±60 g), whose periods and
  phases come from a fixed random seed (42), so the curve is identical every
  run,
- an assumed uncertainty of ±10 % (also a placeholder).

For demos, `intensity-force green` pins it to 80 g/kWh and `intensity-force
dirty` to 700 g/kWh. `intensity-force off` returns to the curve. The choice is
saved in `data/intensity_state.json`, so every command (even a `watch`
already running in another window) picks it up.

The conversion multiplies energy by intensity, and for a product the
**relative** errors combine in quadrature.

Worked example (job above, intensity 450 ± 45 g/kWh at the job's midpoint):

```
gCO2e         = 0.000175 kWh × 450 g/kWh = 0.0788 g
relative err  = √( (61.0/630)² + (45/450)² ) = √(0.0968² + 0.100²) = 0.139  (13.9 %)
absolute err  = 0.0788 × 0.139 = 0.011 g
result        = 0.079 ± 0.011 gCO2e
```

(The code uses the equivalent form `√((I·ΔE)² + (E·ΔI)²)`, which gives the
same answer and also works when the energy is exactly zero.)

### `watch`

`watch` shows the **whole package** live, not a single job. At 27 W, one
second uses 27 J = 0.0000075 kWh:

- dirty (700 g/kWh): 0.0000075 × 700 = **0.00525 g/s**
- green (80 g/kWh): 0.0000075 × 80 = **0.00060 g/s**

Same load, about 9× less carbon. That difference is what later phases will
turn into scheduling and credits.

---

## 7. Everything Phase 1 does *not* capture (for the viva)

1. **CPU package only.** RAM, display, SSD, Wi-Fi, fans and charger losses
   are not measured (the Memory and Platform sensors are dead).
2. **CPU time is an imperfect proxy** for energy on hybrid P/E-core CPUs (see
   section 4).
3. **Very short-lived child processes** (started and finished between two
   0.25 s CPU polls) are missed.
4. **The sensor itself is a model.** RAPL is Intel's on-chip estimate, good
   but not a lab instrument. Hence the sensor tolerance term.
5. **One intensity value per job** (at its midpoint). Fine for short jobs; a
   long job crossing a big change in the grid would need intensity
   integrated over time.
6. **The intensity is simulated.** Real grid data would come from a provider
   such as Electricity Maps (an optional later milestone).
7. **The idle baseline can drift** (temperature, background updates). Re-run
   `calibrate-idle` before a measurement session.
