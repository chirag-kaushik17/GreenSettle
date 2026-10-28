# GreenSettle Phase 3 ("Schedule and enforce"), explained

Phase 1 measures a job's energy and Phase 2 settles it into a ledger. Phase 3
turns them into a live system:

- users submit jobs with a wait tolerance,
- a scheduler picks the greenest start inside that window,
- a worker on the laptop runs the jobs with the Phase 1 meter,
- each job is settled from what was actually measured.

```
 client ──POST /jobs──►  server  ◄──POST /agent/claim──  worker (laptop, next to LHM)
                          │  plan once (choose_slot)          │
                          │  queue + tick (decay, leases)     │ runs job with Phase 1 run_job
                          │  ◄──────POST /agent/result────────┘
                          │  settle_job (Phase 2) → ledger
 client ──GET /me──────►  │
```

## 1. From submit to settle

1. **Submit** (`POST /jobs`, user API key). The body is `{job_type, params,
   est_duration_s, max_delay_h}`, checked against the job catalog. The
   server plans the job **once**, using the forecast issued now
   (`choose_slot`), and stores the plan. The job is now `queued` with a
   `planned_start`, and `latest_start = submit + max_delay_h`.
2. **Tick** (every real second). Applies decay for users with a ready job,
   so tiers are current, and expires leases. Nothing else changes jobs.
3. **Claim** (`POST /agent/claim`, agent token). The worker asks for work. The
   server picks the first job that is ready and allowed to start (see §6),
   marks it `running` and gives it a lease.
4. **Run** (on the laptop). The worker checks the job against its own copy of
   the catalog, builds a fixed argv list, and runs it with the Phase 1 meter.
5. **Result** (`POST /agent/result`). The server converts the real start and
   end times to sim time and computes:
   - `i_run` = mean **actual** intensity over the run;
   - `i_submit` = mean **actual** intensity over the same duration, starting
     at submission (the counterfactual).

   It then calls Phase 2 `settle_job`, which writes the fee, rebate,
   surcharge and decay rows. A repeated result returns the stored settlement.

A job with a non-zero exit code is marked `failed` and **not settled**.

## 2. The slot rule (`choose_slot`), in words

The student implemented this function; the tests in `tests/test_slots.py`
pin down its behaviour.

- The candidates are the submission time and then every 30 minutes, as long
  as the start is still **inside the wait window**. The job may *finish*
  after the window; the window is about waiting to *start*.
- For each candidate, ask the forecast for the mean intensity over the job's
  duration, and its uncertainty.
- The best candidate has the lowest forecast; a tie goes to the earliest.
- The expected saving is forecast(now) − forecast(best). Its uncertainty
  combines both forecasts' uncertainties in quadrature.
- Defer only if the saving is **larger than z × that uncertainty**. Otherwise
  start now and give the reason.

### Worked by hand (30-minute slots, z = 1)

**V1 (4 h window).** N = floor(4 × 3600 / 1800) = 8, so candidates i0 … i8.

- Forecasts: i0 600 ± 60; i1–i3 580 ± 58; i4 and i5 150 ± 15; i6–i8 400 ± 40.
- Lowest is 150, at i4 and i5; the tie goes to the earlier one, **i4**.
- saving = 600 − 150 = 450; uncertainty = √(60² + 15²) = 61.8.
- 450 > 1 × 61.8 → **deferred** to t0 + 4 × 1800 s = t0 + 2 h, expected
  saving 450 g/kWh.

**V2 (4 h window).**

- Forecasts: i0 400 ± 40; i1, i2 395 ± 40; i3 380 ± 38; the rest 400 ± 40.
- Best is i3; saving = 20; uncertainty = √(40² + 38²) = 55.2.
- 20 ≤ 55.2 → **start now**, reason `gain_within_noise`. (With z = 0 the same
  numbers defer to i3: V3.)

**V6 (4 h window).**

- Every forecast is 500 ± 50 except i9 = 50 ± 5.
- i9 starts at t0 + 4.5 h, outside the window, so it is **not a candidate**.
  There are 9 candidates, i0 … i8, all equal.
- The tie goes to i0 → **start now**, reason `already_best`.

### Why not defer when the gain is inside the noise?

Phase 2 asks the same question after the run: settlement pays only for the
part of a saving that exceeds k standard uncertainties. The scheduler asks it
before the run. If the forecast gain is smaller than the forecast's own error,
deferring mostly adds waiting time without reliably saving carbon. A job
deferred for a noise-sized gain would also usually land in the dead zone and
earn nothing.

### choose_slot in words

What `greensettle/scheduler/slots.py` does, step by step, in the order of the
code:

0. **Validate first.** If `max_delay_h < 0`, `est_duration_s ≤ 0`,
   `slot_seconds ≤ 0` or `defer_z < 0`, raise `ValueError` before asking the
   forecast anything.
1. **Count the candidates.** `n = floor(max_delay_h × 3600 / slot_seconds)`.
   The starts are t0, t0 + slot, …, t0 + n × slot.
2. **Ask the forecast once per candidate.** For each start s, call
   `forecast_mean(s, s + est_duration_s)` and keep the triple (start, mean,
   uncertainty).
3. **Find the lowest mean.** Begin with candidate 0 and move to a later
   candidate only if its mean is *strictly* lower, so ties stay with the
   earliest start.
4. **Compute the gain and its noise.** `saving = f0 − f_best` and
   `noise = hypot(u0, u_best)`.
5. **Decide, in this order:**
   - n = 0 → `no_window`;
   - the best is candidate 0 → `already_best`;
   - `saving ≤ defer_z × noise` → `gain_within_noise`;
   - otherwise → `deferred`.

   Only `deferred` moves the job. Every other reason returns candidate 0, so
   `forecast_mean` = f0 and `expected_saving` = 0. All candidates are
   returned for the dashboard.

Traced through the code (30-minute slots, z = 1):

- **V1** (4 h): step 1 gives n = 8, so 9 forecast calls. Step 3 moves the best
  from i0 (600) to i1 (580), then to i4 (150); i5 (also 150) is not strictly
  lower, so i4 stays. Step 4: saving 450, noise hypot(60, 15) = 61.8. Step 5:
  n ≠ 0, best ≠ 0, 450 > 61.8 → `deferred`, start t0 + 7200 s.
- **V2** (4 h): the best moves to i1 (395), then i3 (380). Saving 20, noise
  hypot(40, 38) = 55.2. Step 5: 20 ≤ 55.2 → `gain_within_noise`, start now.
- **V6** (4 h): n = 8, so i9 (50 ± 5, at t0 + 4.5 h) is never requested. All
  nine requested means are 500, so no later candidate is strictly lower and
  the best stays at i0 → `already_best`, start now.

## 3. Pull, not push

The worker polls the server. The server never opens a connection to the
laptop, so the laptop can sit behind NAT or a home router with no open ports.
It also means the server does not need to know the worker's address or trust
anything the worker exposes.

## 4. The simulated clock

Real grid curves change over hours; a demo lasts minutes. The server runs on
simulated time:

```
sim_now = anchor_sim + (real_now − anchor_real) × scale        (--time-scale, e.g. 600)
```

At ×600, one real second is 10 sim minutes. The anchors are saved in the
`meta` table. On restart, sim time is taken to have kept running at the old
scale, and the clock re-anchors there with the new scale.

What uses which time:

- **Sim time:** planning, ledger timestamps, intensities.
- **Real time (epoch seconds):** leases and the worker's start/end times.
- **Conversion:** the user's `est_duration_s` is real seconds, converted to
  sim seconds (× scale) for planning.

## 5. Actual vs forecast intensity, and why the override is a timeline

- **Actual:** the simulated curve, plus demo overrides (green / dirty / off).
  Used for settlement.
- **Forecast:** the same curve **without** overrides, plus a deterministic
  error. The error grows with lead time: 5 % + 2 % per hour, capped at 40 %,
  all placeholders. Used only for planning.

`POST /admin/intensity` **appends** an override starting at the current sim
time. It never changes the past. If a flip rewrote history, the
counterfactual ("what if the job had started at submission?") would change
with it, and a deferral could never be shown to backfire.

**The counterfactual uses actual data, not the forecast.** Settlement
compares what really happened with what would really have happened.
Comparing with the forecast would reward forecast errors instead of real
savings.

## 6. Dispatch order and the starvation guard

Among ready jobs (`planned_start ≤ sim_now`):

1. jobs **past their latest start** go first, and tier is ignored for them;
2. higher tier priority (green 2, standard 1, red 0);
3. earlier planned start;
4. earlier submission.

A job may start only if fewer than `max_concurrent_jobs` (2) are running and
its user is below their tier's limit (green 2, others 1).

The **starvation guard** (rule 1) exists because the wait window is a
promise. Without it, a red-tier user's job could be pushed back forever by
higher tiers.

## 7. Leases

A claim gives the worker a lease of `3 × est_duration_s + 60` real seconds. If
no result arrives in time (the laptop crashed, lost the network, or the job
hung), the tick puts the job back in the queue **once**; a second expiry
marks it `failed`. A result from a worker that no longer holds the lease is
rejected (409). Claims are atomic (`BEGIN IMMEDIATE`): two workers can never
take the same job.

## 8. Security rules

- **Jobs are never commands.** A job is `{job_type, params}` from a fixed
  catalog (`burn`, `hash`, `primes`), and every parameter is an integer in a
  hard range. `burn` is capped at 60 s because the laptop reached 103 °C. The
  **worker** re-validates and builds the argv itself
  (`python -m greensettle.jobs.<type> --name <int> …`), with no shell and no
  user strings. Each job module checks its arguments a third time.
- **Auth:**
  - user API keys are 32 random bytes, shown once and stored as SHA-256;
  - the agent and admin tokens come only from environment variables, and the
    server refuses to start without them;
  - every token check uses `hmac.compare_digest`.
- **Limits:**
  - 20 queued jobs per user;
  - a request body size limit;
  - strict request models: unknown fields, wrong types and NaN are rejected,
    and error messages never echo the input;
  - `est_duration_s` ≤ 120 and `max_delay_h` ≤ 48.
- **Network:** binds 127.0.0.1 unless `--host` is given, and then prints a
  warning that this needs HTTPS or an SSH tunnel (see `deploy/README.md`).
- **Trust model:** the server **trusts the worker's energy numbers**.
  Measurement attestation is out of scope; a dishonest worker could lie.

## 9. LIMITS (be explicit about these in the viva)

- **Plan once, no re-planning.** A job keeps its slot even if a later
  forecast would choose differently.
- **One intensity mean per job.** The run is assumed to use energy evenly
  over its duration. The mean is sampled every sim minute, at most 60 points,
  and its uncertainty is the provider's relative uncertainty (a placeholder).
- **The server trusts worker measurements** (see the trust model).
- **Failed jobs are not settled**, so their energy is never billed. A user
  could crash jobs on purpose to run work for free; the lease and attempts
  limit only bound how often.
- **Tiers use the ledger balance after decay at tick time.** Decay runs in
  the tick only for users with a ready job, and otherwise before
  balance-changing writes.
- **Starvation-guard loophole.** A job with `max_delay_h = 0` is overdue
  almost immediately, so it skips tier priority. Not fixed yet.
- **`jobs.i_submit` is provisional.** The Phase 2 schema requires a value at
  submission, so the actual intensity known then is stored. Settlement
  recomputes the counterfactual from actual data, which an override added
  later can still affect.
- **All defaults are placeholders:** forecast error, caps, lease length,
  tiers, allowance, decay, settlement parameters, and the catalog's ranges.
