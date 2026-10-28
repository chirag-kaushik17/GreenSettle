# GreenSettle viva cheat-sheet

Short answers to likely examiner questions, each with the file and function to
open if asked to show it. For numbers, quote `experiments/results/SUMMARY.md`
(regenerate it with `python experiments/run_offline.py`). Never quote a number
from memory.

---

**1. Why a dead zone at all?**
Every saving is computed from noisy numbers: measured energy and two intensities.
With honest error bars, a job that saved nothing still shows a positive "saving"
about half the time. The dead zone pays only for the part of a saving beyond k
standard uncertainties, so noise alone rarely earns a rebate (E4, the gaming table).
→ `greensettle/settlement/rules.py` `compute_settlement` (step 3); `experiments/e4_deadzone.py`

**2. Why pay on the lower bound (saved − kσ) instead of the saving itself?**
The rebate then grows smoothly from zero at the edge of the dead zone. There is no
cliff where a tiny extra "saving" suddenly unlocks a large payment, so there is
nothing to game at the threshold. It also means we pay only for what is proven
at the chosen confidence.
→ `rules.py` `compute_settlement`: `saved_low = saved - k * saved_unc`

**3. Why is the rebate capped below the fee (γ < 1)?**
The rebate is at most γ × fee, so net = fee + surcharge − rebate ≥ (1 − γ) × fee.
That is positive whenever the job used any energy, so running jobs can never earn
credits, however lucky the noise. The ledger rounding keeps this true in integers.
→ `rules.py` (the `min(..., gamma * fee)`); `greensettle/ledger/credits.py` `settlement_to_micro`

**4. Why does the counterfactual use actual grid data, not the forecast?**
We want what really happened versus what would really have happened. With the
forecast, users would be rewarded or punished for forecast errors, not for real
savings. i_submit is the actual mean intensity over the same duration, starting at
submission.
→ `greensettle/server/app.py` `result` endpoint; `greensettle/intensity/timeline.py` `mean_intensity`

**5. Why is the demo override a timeline instead of a switch?**
An override applies only from the moment it is added. If flipping to "dirty"
rewrote the past, the counterfactual would change with it, and a deferral could
never be shown to backfire. Appending before the latest override is rejected.
→ `timeline.py` `OverrideTimeline.add`; `greensettle/server/store.py` `append_override`

**6. Why does the worker pull work instead of the server pushing it?**
The laptop can sit behind NAT or a home router with no open ports. The server never
needs the worker's address, and a worker that disappears simply stops claiming.
Leases recover jobs from a worker that died mid-run.
→ `greensettle/agent/worker.py` `Worker.claim`; `greensettle/scheduler/queue.py` `claim_job`, `expire_leases`

**7. What is your trust model?**
The server trusts the energy numbers the worker reports; attestation is out of
scope, so a dishonest worker could lie. What *is* enforced: plausible ranges
(non-negative, finite, cpu_share in [0, 1]) and a lease the worker must hold.
Jobs come only from a fixed catalog.
→ `app.py` `ResultIn`; `docs/phase3_explained.md` section 8

**8. What does the power sensor miss?**
Only the CPU package is measured (scope `cpu_package`). RAM, screen, disk, network
and charger losses are not; the laptop's "CPU Memory" and "CPU Platform" sensors
read 0 W. The package value is itself a model (RAPL), which is why there is a
sensor tolerance term.
→ `greensettle/agent/lhm_client.py` `read_cpu_powers`; `greensettle/agent/meter.py` `MeterConfig`

**9. How do you split the energy between your job and everything else running?**
Subtract the calibrated idle energy. Then give the job its share of the dynamic
energy by CPU time: job CPU-seconds (including all child processes) over all busy
CPU-seconds in the same window.
→ `meter.py` `split_energy`, `ProcessTreeCpuTracker`, `system_busy_cpu_seconds`

**10. What is the hybrid-core limitation?**
On this Intel Core Ultra, a second on a P-core costs more energy than a second on
an E-core. Splitting by CPU time treats them as equal, so a job that ran mostly on
P-cores is under-charged. This is only covered by the attribution uncertainty term
(a placeholder), not corrected.
→ `meter.py` `split_energy` docstring; `MeterConfig.attribution_base_fraction`

**11. How is a job's energy uncertainty computed?**
Sensor tolerance, sampling error (trapezoid vs left Riemann), idle-baseline noise
and attribution error, combined in quadrature. The first three are scaled by
cpu_share, because only that share of the package and idle errors reaches the job.
Quadrature assumes independent errors.
→ `meter.py` `estimate_uncertainty`

**12. Why does choose_slot defer only if the gain beats the noise?**
The same principle as the dead zone, applied before the run. If the forecast saving
is within the forecast's own uncertainty, deferring mostly adds waiting time and
risks a backfire. With defer_z > 0, E3 shows fewer deferrals and a smaller share of
backfired deferrals.
→ `greensettle/scheduler/slots.py` `choose_slot` step 5; `experiments/e3_scheduling.py`

**13. Why ex-post rather than ex-ante settlement?**
Ex-ante rewards are paid on declarations and forecasts before anything is measured,
so they can pay for savings that never happened. In the gaming table, the
simplified ex-ante strawman paid some jobs to run, and no ex-post rule did. It is a
strawman, not a real scheme, and the output says so.
→ `experiments/gaming_table.py`

**14. What happens to failed jobs?**
A non-zero exit code marks the job `failed` and it is **not settled**: no fee, no
rebate, no ledger rows. So its energy is never billed, and a user could crash jobs on
purpose. The lease and attempt limit only bound how often. This is a stated limit.
→ `app.py` `result` (the `exit_code != 0` branch)

**15. Why integer micro-credits, and why round against the user?**
Integers do not drift when added up (floats do). Fee and surcharge round up, the
rebate rounds down and is capped at floor(γ × fee_micro), so rounding can never turn
into free credits.
→ `ledger/credits.py` `settlement_to_micro`

**16. How is the ledger append-only, and where is the balance?**
SQLite triggers abort any UPDATE or DELETE on the ledger table. The balance is never
stored: it is always SUM(amount_micro), and a view adds a running balance. Only five
row kinds are allowed (CHECK constraint), and there are unique indexes per allowance
period and per (job, kind).
→ `greensettle/ledger/migrations.py` (schema and triggers); `greensettle/ledger/db.py` `balance_micro`

**17. Why decay and tiers?**
Decay (a half-life on positive balances, applied lazily before each balance-changing
write) stops hoarding. Tiers turn the balance after decay into queue priority and
concurrency, so users who save carbon get served first. Debt does not decay.
→ `ledger/db.py` `decay_in_transaction`; `greensettle/ledger/config.py` `tier_for`

**18. Can a low-tier user be starved?**
No: a job past its latest start goes first regardless of tier, because the wait
window is a promise. Known loophole: a job submitted with no wait window is overdue
almost immediately and so skips tier priority.
→ `greensettle/scheduler/dispatch.py` `dispatch_order`

**19. Why a simulated clock, and what happens on restart?**
Real grid curves change over hours, and a demo lasts minutes, so sim time runs
`scale` times faster. Scheduling and the ledger use sim time; leases use real time.
On restart, sim time continues as if the clock had kept running at the old scale.
→ `greensettle/server/clock.py` `Clock`, `Clock.resume`; `store.py` `load_clock`

**20. What is real and what is simulated, and how do you know it works?**
Simulated: the grid curve, overrides, the forecast error, the noise models and the
job energies in E3, E4 and the gaming table. Real: CPU package power and job CPU time
on one laptop; E1 is the real-hardware check and is PENDING until run there. The test
suite runs without hardware; the README table lists what was verified where.
→ `README.md` (Verification status); `experiments/results/SUMMARY.md`

---

**If asked "what is new?"** Only this: *ex-post counterfactual settlement with
propagated measurement uncertainty and a dead zone*. Say that only a limited
literature search was done.
