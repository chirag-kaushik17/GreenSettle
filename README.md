# GreenSettle: Phase 1 "Measure"

GreenSettle is a carbon-credit system for cloud workloads. Jobs run in greener
time slots, and credits are settled **after** the run from **measured**
energy. Phase 1 builds only the measurement layer:

- read CPU power from LibreHardwareMonitor,
- turn it into per-job energy with an uncertainty estimate,
- convert that to gCO2e with an error band.

The plain-language explanation with a worked numeric example is in
[`docs/phase1_explained.md`](docs/phase1_explained.md).

## Layout

```
greensettle/                 <- project root (run all commands from here)
  greensettle/               <- the Python package
    agent/lhm_client.py      fetch + parse LHM data.json
    agent/meter.py           sampler, idle calibration, integration, attribution, uncertainty
    intensity/provider.py    simulated carbon-intensity curve + demo override
    engine/convert.py        kWh + intensity -> gCO2e with error band
    cli.py                   command-line interface
  tools/burn.py              CPU burner (--seconds, --threads; capped at 60 s)
  tests/                     pytest suite + mock LHM server (tests/mock_lhm.py)
  fixtures/lhm_data_sample.json   SYNTHETIC sample, see fixtures/README.md
  docs/phase1_explained.md
  data/                      runtime output (git-ignored): idle.json, measurements/, intensity_state.json
```

The package sits one level down (`greensettle/greensettle/`). That way
`python -m greensettle.cli` and `python tools/burn.py` both work from the
project root.

## Setup (Windows 11, PowerShell, native Python 3.11+)

```powershell
cd greensettle
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

LibreHardwareMonitor must be running **as administrator** with
*Options → Remote Web Server → Run* enabled on port 8085. To use a different
URL, pass `--url http://host:port/data.json` or set the
`GREENSETTLE_LHM_URL` environment variable.

## Commands

```powershell
python -m greensettle.cli check-sensor                  # server reachable? which sensors live/dead?
python -m greensettle.cli dump-powers --save fixtures\lhm_data_sample.json   # capture real data.json
python -m greensettle.cli calibrate-idle --seconds 30   # leave the laptop alone meanwhile
python -m greensettle.cli run -- python tools/burn.py --seconds 30 --threads 4
python -m greensettle.cli intensity-force dirty         # or: green | off
python -m greensettle.cli watch                         # live W and gCO2e/s, Ctrl+C to stop
```

Global options (put them **before** the command): `--url`, `--data-dir`
(default `data`), `--interval` (poll seconds, default 1.0), `--intensity`
(default `simulated`), `-v`.

`run` writes `data/measurements/<job_id>.json` in the stable schema that later
phases will read. The scope is `cpu_package`, because the Memory and Platform
sensors are dead on this laptop.

## Tests

```powershell
python -m pytest                                    # hardware tests are skipped
$env:GREENSETTLE_HARDWARE = "1"; python -m pytest   # also runs tests/test_hardware.py
```

The tests use `tests/mock_lhm.py`, a fake LHM web server, so they pass on any
OS. You can also run the mock by hand for a demo without the real sensor:

```powershell
python tests/mock_lhm.py --follow-load     # fake CPU Package power follows real CPU load
```

## Placeholders

All uncertainty coefficients are in `MeterConfig` (`greensettle/agent/meter.py`)
and are **placeholders**, not measured values. The simulated intensity curve
is **not** real grid data.

## Verification status

| what | verified on |
|---|---|
| parser, integration, attribution, uncertainty, intensity, conversion, CLI | pytest, mock server (Linux) |
| psutil CPU-time tracking (incl. exited children) | real processes (Linux) |
| `check-sensor`, `calibrate-idle`, `run`, `watch` against the **real** LHM on the Windows laptop | **not yet** |
| `fixtures/lhm_data_sample.json` matches real LHM 0.9.6 output | **not yet** (synthetic) |
