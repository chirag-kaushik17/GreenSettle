"""The worked settlement examples (test vectors A, C, D) computed by the REAL
compute_settlement, saved for the figures, poster and report.

    python experiments/worked_examples.py   ->  experiments/results/worked_examples.json

E = 0.000175 +/- 0.0000169 kWh (the Phase 1 worked example); default
(placeholder) parameters. The intensities are illustrative inputs, not data.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from greensettle.settlement.rules import SettlementParams, compute_settlement  # noqa: E402

E, DE = 0.000175, 0.0000169
CASES = {  # name: (i_submit, i_submit_unc, i_run, i_run_unc)
    "A (big saving)": (700, 70, 80, 8),
    "C (inside the noise)": (82, 8.2, 80, 8),
    "D (backfire)": (80, 8, 700, 70),
}


def run() -> dict:
    params = SettlementParams()
    out = {"energy_kwh": E, "energy_unc_kwh": DE, "params": asdict(params), "cases": []}
    for name, (i_s, u_s, i_r, u_r) in CASES.items():
        s = compute_settlement(E, DE, i_s, u_s, i_r, u_r, params)
        out["cases"].append({"name": name, "i_submit": i_s, "i_submit_unc": u_s, "i_run": i_r, "i_run_unc": u_r,
                             **asdict(s)})
    return out


def main() -> int:
    path = ROOT / "experiments" / "results" / "worked_examples.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = run()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for c in data["cases"]:
        print(f"{c['name']:22} zone {c['zone']:9} saved {c['saved_g']:+.4f} ± {c['saved_unc_g']:.4f} g  "
              f"fee {c['fee']:.4f}  rebate {c['rebate']:.4f}  net {c['net']:.4f}")
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
