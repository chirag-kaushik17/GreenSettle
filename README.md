# GreenSettle

A carbon-credit system for cloud workloads. Users submit batch jobs with a
declared wait tolerance, jobs run in greener time slots, and credits are
settled **after** the run from **measured** energy.

| phase | what | explained in |
|---|---|---|
| 1 Measure | CPU power → per-job energy ± uncertainty → gCO2e ± error | [docs/phase1_explained.md](docs/phase1_explained.md) |
| 2 Settle | settlement rule, append-only ledger, decay, tiers, gaming table | [docs/phase2_explained.md](docs/phase2_explained.md) |
| 3 Schedule and enforce | API server, slot choice, queue, worker, client, demo | [docs/phase3_explained.md](docs/phase3_explained.md) |
| 4 Show and prove | dashboard, experiments E1/E3/E4, figures, poster, report | [docs/viva_cheatsheet.md](docs/viva_cheatsheet.md) |

Two functions are written by the student and are the core of the viva:
`compute_settlement` (Phase 2) and `choose_slot` (Phase 3). Both are
implemented.

## Layout

```
greensettle/                      <- project root (run all commands from here)
  greensettle/                    <- the Python package
    agent/lhm_client.py           fetch + parse LibreHardwareMonitor data.json
    agent/meter.py                sampler, idle calibration, integration, attribution, uncertainty
    agent/worker.py               Phase 3 worker (pulls jobs, runs them with the meter)
    intensity/provider.py         simulated carbon-intensity curve
    intensity/timeline.py         actual (with override timeline) vs forecast providers
    engine/convert.py             kWh + intensity -> gCO2e with error band
    settlement/rules.py           compute_settlement (student)
    ledger/                       config, migrations, db (decay, allowance, tiers), settle_job, CLI commands
    scheduler/slots.py            choose_slot (student)
    scheduler/{planner,dispatch,queue}.py   plan once, dispatch order, queue/claim/leases
    jobs/                         the fixed job catalog: burn, hash, primes
    server/                       FastAPI app, config, clock, store; python -m greensettle.server
    client.py                     python -m greensettle.client
    cli.py                        Phase 1 + 2 command line
  experiments/gaming_table.py     can users farm credits? (results in experiments/results/)
  experiments/e2e_demo.py         narrated end-to-end demo
  tools/burn.py                   Phase 1 CPU burner (capped at 60 s)
  tests/                          pytest suite, mock LHM server, fakes
  fixtures/lhm_data_sample.json   SYNTHETIC sample, see fixtures/README.md
  deploy/README.md                how a cloud deployment WOULD work (docs only)
  data/                           runtime output (git-ignored)
```

## Setup (Windows 11, PowerShell, native Python 3.11+)

```powershell
cd greensettle
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

LibreHardwareMonitor must run **as administrator** with
*Options → Remote Web Server → Run* on port 8085 (or set `--url` /
`GREENSETTLE_LHM_URL`).

## Phase 1: measure

```powershell
python -m greensettle.cli check-sensor
python -m greensettle.cli dump-powers --save fixtures\lhm_data_sample.json   # capture the REAL data.json
python -m greensettle.cli calibrate-idle --seconds 30
python -m greensettle.cli run -- python tools/burn.py --seconds 30 --threads 4
python -m greensettle.cli intensity-force dirty      # green | dirty | off
python -m greensettle.cli watch
```

## Phase 2: settle (offline, no server)

```powershell
python -m greensettle.cli user-add alice
python -m greensettle.cli allowance alice
python -m greensettle.cli settle data\measurements\<job>.json --user alice `
    --submit-time 2026-11-02T08:00:00+00:00 --run-time 2026-11-02T13:00:00+00:00
python -m greensettle.cli balance alice
python -m greensettle.cli ledger alice --limit 10
python experiments\gaming_table.py
```

Use `--clock ISO` (or `$env:GREENSETTLE_CLOCK`) to set the simulated "now".

## Phase 3: three terminals

```powershell
# tokens (once): python -c "import secrets; print(secrets.token_hex(32))"
$env:GREENSETTLE_AGENT_TOKEN = "<token 1>"; $env:GREENSETTLE_ADMIN_TOKEN = "<token 2>"

# terminal 1: server (binds 127.0.0.1)
python -m greensettle.server --time-scale 600

# terminal 2: worker, next to LibreHardwareMonitor (add --mock-lhm for a hardware-free run)
python -m greensettle.agent.worker --server http://127.0.0.1:8000 --agent-id laptop

# terminal 3: client
python -m greensettle.client admin-user alice                  # copy the api_key it prints
python -m greensettle.client admin-allowance alice
$env:GREENSETTLE_API_KEY = "<alice's key>"
python -m greensettle.client submit --type burn --param seconds=20 --param workers=2 --max-delay-h 6 --est-duration-s 25
python -m greensettle.client status <job_id>
python -m greensettle.client me
python -m greensettle.client admin-intensity dirty             # demo flip, from the server's sim "now"

# or everything in one process:
python experiments\e2e_demo.py --mock-lhm --time-scale 600
```

## Phase 4: dashboard

Open `http://127.0.0.1:8000/dashboard` while the server runs and paste the
admin token (kept in page memory only; never stored). It polls every 2 s.

- **Clock and grid:** sim time, the 24 h actual vs forecast intensity
  (simulated), override periods.
- **Live power:** watts and gCO2e/s.
- **Queue:** candidate slots of a queued job.
- **Settlements:** zone, money and saved ± uncertainty.
- **Users:** balance, tier, decay, sparkline.
- **Explain:** the arithmetic of one settlement in words.

One file, no external resources, served with a strict Content-Security-Policy.

## Phase 4: experiments, figures, poster, report

```powershell
pip install -r requirements-report.txt               # matplotlib, python-docx (only for figures/report)

python experiments\run_offline.py                    # worked examples, gaming table, E3, E4 -> experiments\results\SUMMARY.md
python -m greensettle.cli calibrate-idle --seconds 30 # on the laptop, machine idle
python experiments\e1_repeatability.py --repeats 6 --seconds 20 --workers 1,4   # REAL laptop, about 6 min
python experiments\e1_repeatability.py --mock-lhm --repeats 2 --seconds 2 --cooldown 0 --rest 0   # dry run, NOT evidence

python report\make_figures.py                        # report\figures\*.png (from results files only)
python report\make_poster.py                         # report\poster.html (+ poster.pdf if Playwright is installed)
python report\make_report.py                         # report\GreenSettle_report.docx (open in Word, update fields)
```

- **E3** (simulated) runs the real `choose_slot` over 7 simulated days.
- **E4** (simulated) sweeps the dead-zone width k.
- **E1** measures repeatability on the real laptop. Until `e1.json` exists,
  the poster and report show "PENDING (run on the laptop)". Mock dry runs
  write `e1_mock.*`, which are never used as evidence.
- Every number in the figures, poster and report is read from
  `experiments/results/` or from the code's config, via `report/facts.py`.
- The poster PDF needs Playwright (`pip install playwright && playwright
  install chromium`). Otherwise, print `poster.html` from Chrome at
  420 × 594 mm with background graphics on.
- For the viva, see [`docs/viva_cheatsheet.md`](docs/viva_cheatsheet.md).

## Tests

```powershell
python -m pytest                                    # hardware tests are skipped
$env:GREENSETTLE_HARDWARE = "1"; python -m pytest   # also runs tests/test_hardware.py on the laptop
```

The tests use a mock LHM and a fake clock. Some queue and API tests use a
stand-in `choose_slot` from `tests/fakes.py` so they can fix the plan;
`tests/test_slots.py` tests the real `choose_slot`.

## Verification status

Everything so far was built and run in a **Linux cloud container**, not on
the Windows laptop.

| what | real laptop | mock / container only |
|---|---|---|
| LHM `data.json` structure (fixture is synthetic) | not yet | ✓ |
| `check-sensor`, `calibrate-idle`, `run`, `watch` with the real sensor | not yet | ✓ (mock LHM) |
| job CPU-time accuracy (`os.wait4` fix is POSIX-only; Windows keeps the tracker) | not yet | ✓ Linux: 118.8 of 120 CPU-s |
| `compute_settlement` vectors and properties | n/a (pure code) | ✓ |
| ledger, decay, tiers, allowance, `settle_job`, Phase 2 CLI | not yet | ✓ |
| gaming table | n/a (simulation) | ✓ |
| server, auth, limits, claim/leases, settlement on result | not yet | ✓ (in-process and real uvicorn) |
| worker + client over HTTP | not yet | ✓ (mock LHM) |
| e2e demo (all steps, real `choose_slot`) | not yet | ✓ (mock LHM) |
| dashboard | not yet | ✓ rendered in headless Chromium (light, dark, 390 px) against a scratch server with stand-in plans |
| `choose_slot` (19 tests) | n/a (pure code) | ✓ |
| E3 scheduling, E4 dead zone, worked examples | n/a (simulation) | ✓ results in `experiments/results/` |
| E1 repeatability | **PENDING**: run on the laptop | dry run only (`--mock-lhm`, not evidence) |
| figures, poster (A2, 1 page), report (.docx, 9 pages via LibreOffice) | generate again after E1 | ✓ built in the container |
