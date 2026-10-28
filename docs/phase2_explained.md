# GreenSettle Phase 2 ("Settle"), explained

Phase 1 measures a job's energy (kWh ± uncertainty). Phase 2 turns that
measurement into credits and books them in a ledger:

```
measurement (E ± dE) + intensities (i_submit ± u, i_run ± u)
        │
        ▼
compute_settlement()            greensettle/settlement/rules.py   (pure arithmetic)
        │  zone, fee, rebate, surcharge, net, saved_g, saved_unc_g
        ▼
settlement_to_micro()           greensettle/ledger/credits.py     (rounding)
        │
        ▼
settle_job()                    greensettle/ledger/settle.py      (one transaction)
        decay → settlements row → fee / rebate / surcharge ledger rows
```

Units: energy in kWh, intensity in g CO2e/kWh, emissions in g CO2e, money in
**credits**. With `p = 1000`, 1 credit is 1 mg CO2e.

---

## 1. The settlement rule: `compute_settlement`

`compute_settlement(energy_kwh, energy_unc_kwh, i_submit, i_submit_unc, i_run, i_run_unc, params)`
returns a `Settlement` (zone, fee, rebate, surcharge, net, saved_g, saved_unc_g).
The parameters are `p`, `rho`, `gamma`, `k` and `sigma` (defaults 1000, 0.5, 0.6, 1, 0).

**Validation.** Every parameter must be finite. `gamma` must be in [0, 1),
`p` must be > 0, and `rho`, `k`, `sigma` must be ≥ 0. All six inputs must be
finite and ≥ 0. Anything else raises `ValueError`.

**Fee on actual emissions.** `fee = p × E × i_run`. The user pays for what the
run really emitted, at the intensity during the run.

**The saving and how uncertain it is.** `saved = E × (i_submit − i_run)` is the
grams saved compared with running immediately at submission time (negative
means the run emitted more). Its uncertainty adds three independent errors in
quadrature:

```
saved_unc = √( (dE × (i_submit − i_run))²     energy error
             + (E × i_submit_unc)²            counterfactual intensity error
             + (E × i_run_unc)² )             actual intensity error
```

**Dead zone.** The rule only acts on what is beyond `k` standard uncertainties:

```
saved_low = saved − k × saved_unc        pessimistic saving
extra_low = −saved − k × saved_unc       pessimistic extra emissions
```

- `saved_low > 0` → zone **saving**: `rebate = min(rho × p × saved_low, gamma × fee)`.
- otherwise, `extra_low > 0` → zone **backfire**: `surcharge = sigma × p × extra_low`
  (0 with the default `sigma = 0`).
- otherwise → zone **dead_zone**: no rebate, no surcharge.

`net = fee + surcharge − rebate`.

**The rebate cap is below the fee.** The rebate is at most `gamma × fee`, and
`gamma < 1`, so the net cost is at least `(1 − gamma) × fee`. A job with a
positive fee always costs something, so running jobs can never earn credits.

**Backfire.** If the run was provably dirtier than running immediately, the
fee is already higher (it uses `i_run`). An extra surcharge is charged only
if `sigma > 0`, and only on the pessimistic extra emissions.

**With `k = 0`** there is no dead zone: any positive measured saving earns a
rebate. This is the naive "ex-post" scheme used for comparison.

### Worked example (test vector A)

E = 0.000175 kWh ± 0.0000169, i_submit = 700 ± 70, i_run = 80 ± 8, default parameters.

| step | calculation | result |
|---|---|---|
| fee | 1000 × 0.000175 × 80 | 14.0 credits |
| saved | 0.000175 × (700 − 80) | 0.1085 g |
| saved_unc | √((0.0000169 × 620)² + (0.000175 × 70)² + (0.000175 × 8)²) = √(0.010478² + 0.01225² + 0.0014²) | 0.01618 g |
| saved_low | 0.1085 − 1 × 0.01618 | 0.09232 g > 0 → **saving** |
| rebate | min(0.5 × 1000 × 0.09232, 0.6 × 14.0) = min(46.16, 8.4) | 8.4 (the cap) |
| net | 14.0 + 0 − 8.4 | **5.6 credits** |

The tests check seven such vectors (A–G), one for each zone and parameter
edge, plus properties over 2,500 random cases.

---

## 2. Rounding to the ledger (`credits.py`)

The ledger stores whole **micro-credits** (1 credit = 1,000,000), as SQLite
INTEGERs, so balances never drift the way repeated float additions do.
Converting the rule's floats never favours the user:

- fee and surcharge round **up** (`ceil`),
- rebate rounds **down** (`floor`) and is capped at `floor(gamma × fee_micro)`,
  so `rebate ≤ gamma × fee` still holds after rounding,
- net is computed from the rounded parts, so the ledger rows add up exactly.

Vector A becomes fee 14,000,000, rebate 8,400,000 and net 5,600,000 micro-credits.

---

## 3. The ledger

| rule | how it is enforced |
|---|---|
| append-only | SQLite triggers make every UPDATE or DELETE on `ledger` fail |
| balance = SUM(amount_micro) | never stored; the view `ledger_with_balance` shows a running total |
| integers only | `CHECK (typeof(amount_micro) = 'integer')` |
| one allowance per user and month | UNIQUE index on (user_id, kind, period_key) |
| a job is charged once | UNIQUE index on (job_id, kind); `settlements.job_id` is a primary key |
| atomic operations | every operation runs in one `BEGIN IMMEDIATE` transaction |
| deterministic | every function takes `now`; only the CLI reads the real clock |

Row kinds: exactly `allowance` (+), `decay` (−), `fee` (−), `rebate` (+),
`surcharge` (−). The database's CHECK constraint rejects anything else.
Balances may go negative (debt). The user is then in the red tier until the
next allowance.

### settle_job

1. Check the job exists and belongs to the user.
2. If it was already settled, return the stored result and write nothing
   (idempotent).
3. `compute_settlement`, then rounding.
4. In one transaction: apply decay, insert the `settlements` row (inputs,
   zone, saved_g, saved_unc_g, the micro-credit amounts, and `params_json`
   with the k that was used), then append fee / rebate / surcharge rows,
   skipping zeros.

The dashboard can show the pessimistic saving as `saved_g − k × saved_unc_g`
from the stored values. Nothing outside `compute_settlement` recomputes it.

### Decay (lazy)

Unused credits lose value over time, so hoarding does not pay. Decay is
applied right before every write that changes a balance (allowance,
settlement), and by the Phase 3 scheduler tick for users with ready jobs:

```
elapsed_days = now − last_decay_at          (≤ 0 → do nothing)
decay        = floor(balance × (1 − 0.5 ** (elapsed_days / 30)))   if balance > 0
```

A decay row is appended if it is at least 1 micro-credit, and
`last_decay_at = now` is set in every case. Debt does not decay. Example:
100 credits untouched for 30 days lose 50.

### Tiers (from the balance after decay)

| tier | balance (credits) | priority | max running jobs |
|---|---|---|---|
| red | < 20, including debt | 0 | 1 |
| standard | 20 ≤ b < 100 | 1 | 1 |
| green | ≥ 100 | 2 | 2 |

### Allowance

100 credits per calendar month (period `YYYY-MM`, in UTC). Granting the same
month again writes nothing and reports "already granted". Decay is applied
just before a grant.

All numbers in this section are **placeholders**, kept in one dataclass
(`LedgerConfig` in `greensettle/ledger/config.py`).

---

## 4. CLI (offline, no server)

```
python -m greensettle.cli user-add alice
python -m greensettle.cli allowance alice [--period 2026-11] [--amount 100]
python -m greensettle.cli settle data/measurements/<job>.json --user alice \
    --submit-time 2026-11-02T19:00:00+00:00 --run-time 2026-11-02T13:00:00+00:00
python -m greensettle.cli balance alice
python -m greensettle.cli ledger alice [--limit 20]      # newest first
```

Global options: `--db` (default `data/greensettle.db`) and `--clock ISO`, the
simulated "now" for demos. Without `--clock`, the environment variable
`GREENSETTLE_CLOCK` is used, and without that the real clock. Times must
include a time zone (`+00:00`, `Z` or e.g. `+05:30`).
`settle` takes i_run and i_submit from the simulated curve, as the mean over
the job's duration starting at each time. It uses the un-forced curve: a
forced green/dirty state would give both times the same value.

---

## 5. Gaming analysis

*Pending: this section's specification has not been received yet.*
