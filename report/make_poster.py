"""Generate the A2 poster: report/poster.html (one file) and report/poster.pdf.

    python experiments/run_offline.py
    python report/make_figures.py
    python report/make_poster.py

Every number comes from report/facts.py (results files + code config).
The PDF is printed with Playwright if it is available (Python package
`playwright`, or Node's `playwright`); otherwise open poster.html in Chrome
and print to PDF at 420 x 594 mm with background graphics on.
"""

from __future__ import annotations

import base64
import html
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import facts  # noqa: E402

OUT_HTML, OUT_PDF = HERE / "poster.html", HERE / "poster.pdf"


def img(name: str) -> str:
    data = base64.b64encode((facts.FIGURES / name).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def build_html() -> str:
    cap = facts.captions()
    head = facts.headlines()
    placeholders = "".join(f"<tr><td>{esc(n)}</td><td>{esc(v)}</td></tr>" for n, v, _ in facts.placeholders())
    simulated = "".join(f"<li>{esc(s)}</li>" for s in facts.SIMULATED)
    e1_class = "pending" if "PENDING" in head["e1"] else ""

    def figure(file: str, key: str, cls: str = "") -> str:
        return (f'<figure class="{cls}"><img src="{img(file)}" alt="{esc(cap[key])}">'
                f'<figcaption>{esc(cap[key])}</figcaption></figure>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>GreenSettle poster</title>
<style>
@page {{ size: 420mm 594mm; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: #fcfcfb; color: #0b0b0b; }}
body {{ width: 420mm; height: 594mm; overflow: hidden; padding: 13mm 14mm 10mm;
        font: 13.5pt/1.38 system-ui, -apple-system, "Segoe UI", sans-serif; }}
header {{ border-bottom: 1.6mm solid #2a78d6; padding-bottom: 4mm; margin-bottom: 6mm; }}
h1 {{ font-size: 46pt; margin: 0 0 2mm; line-height: 1.05; }}
.sub {{ font-size: 19pt; color: #52514e; }}
.author {{ font-size: 12pt; color: #898781; margin-top: 2mm; }}
.top {{ display: grid; grid-template-columns: 1fr 1.25fr 1fr 1fr; gap: 6mm; margin-bottom: 6mm; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6mm; align-items: start; }}
section {{ background: #fff; border: 0.4mm solid #e1e0d9; border-radius: 3mm; padding: 4.5mm 5.5mm; margin-bottom: 6mm; }}
.top section {{ margin-bottom: 0; }}
h2 {{ font-size: 20pt; margin: 0 0 2mm; }}
p {{ margin: 0 0 2mm; }}
ol, ul {{ margin: 0; padding-left: 6mm; }}
figure {{ margin: 0 0 2.5mm; }}
figure img {{ width: 100%; display: block; }}
.narrow img {{ width: 86%; margin: 0 auto; }}
figcaption {{ font-size: 11pt; color: #52514e; margin-top: 1mm; }}
.rule {{ font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12.5pt; background: #f4f3ef;
         padding: 3mm 4mm; border-radius: 2mm; line-height: 1.55; margin-bottom: 2mm; }}
.headline {{ font-size: 13pt; margin: 1.5mm 0; padding-left: 3.5mm; border-left: 1.2mm solid #2a78d6; }}
.pending {{ border-left-color: #d03b3b; font-weight: 600; }}
table {{ border-collapse: collapse; width: 100%; font-size: 11pt; }}
td {{ border-bottom: 0.3mm solid #e1e0d9; padding: 1mm 1.5mm; vertical-align: top; }}
td:first-child {{ white-space: nowrap; font-weight: 600; }}
.tag {{ display: inline-block; font-size: 10.5pt; border: 0.3mm solid #898781; border-radius: 9mm; padding: 0 3mm;
        color: #52514e; vertical-align: 3pt; }}
.small {{ font-size: 11.5pt; color: #52514e; }}
</style></head>
<body>
<header>
  <h1>GreenSettle</h1>
  <div class="sub">Carbon credits for cloud jobs, settled <b>after</b> the run from <b>measured</b> energy, with honest error bars</div>
  <div class="author">TODO: name · course · university</div>
</header>
<div class="top">
  <section><h2>Problem</h2>
  <p>Carbon-aware scheduling moves jobs to greener hours, but credits are usually granted from forecasts and estimates.
  Nobody checks afterwards whether a job really saved anything, so noise and forecast errors can earn rewards.</p></section>
  <section><h2>Idea in three lines</h2>
  <ol><li>Defer a job to the greenest slot in its wait window, only if the forecast gain beats its noise.</li>
  <li>Measure the job's CPU energy, with uncertainty, while it runs.</li>
  <li>Settle after the run against the same job started at submission (actual grid data); pay only for savings beyond the noise.</li></ol></section>
  <section><h2>Contribution</h2>
  <p>{esc(NOVELTY_TEXT)}</p><p class="small">{esc(facts.LIT_NOTE)}</p></section>
  <section><h2>What is simulated</h2><ul class="small">{simulated}</ul>
  <p class="small" style="margin-top:2mm">Real: CPU package power (LibreHardwareMonitor) and job CPU time (psutil), one laptop.</p></section>
</div>
<div class="grid">
<div>
  <section><h2>Architecture</h2>{figure("fig_a_architecture.png", "architecture")}</section>

  <section><h2>The settlement rule in four lines</h2>
  <div class="rule">fee&nbsp;&nbsp;&nbsp;= p·E·i_run<br>
  saved = E·(i_submit − i_run) ± σ<br>
  if saved − kσ &gt; 0: rebate = min(ρ·p·(saved − kσ), γ·fee)<br>
  net&nbsp;&nbsp;&nbsp;= fee + surcharge − rebate ≥ (1−γ)·fee</div>
  <p class="small">σ combines the energy and both intensity errors in quadrature. Inside the dead zone (|saved| ≤ kσ)
  nothing is paid back or added. i_submit is the counterfactual from actual data; a surcharge applies only to a proven
  backfire and is off by default.</p>
  {figure("fig_b_deadzone.png", "deadzone")}</section>

  <section><h2>Limits</h2><ul>
  <li>CPU package only: RAM, screen, disk and network are not measured.</li>
  <li>CPU-time attribution ignores that P-core and E-core seconds cost different energy.</li>
  <li>The server trusts the worker's measurements (no attestation).</li>
  <li>Jobs are planned once; failed jobs are not settled (their energy is unbilled).</li>
  <li>One mean intensity per job; one laptop; all parameters are placeholders.</li></ul></section>
</div>
<div>
  <section><h2>Results <span class="tag">simulated unless stated</span></h2>
  {figure("fig_c_e3.png", "e3", "narrow")}
  <p class="headline">{esc(head["e3"])}</p>
  {figure("fig_d_e4.png", "e4", "narrow")}
  <p class="headline">{esc(head["e4"])}</p>
  <p class="headline">{esc(head["gaming"])}</p>
  <p class="headline {e1_class}">{esc(head["e1"])}</p></section>

  <section><h2>Placeholders</h2><table>{placeholders}</table></section>
</div>
</div>
</body></html>
"""


NOVELTY_TEXT = f"What may be new here: {facts.NOVELTY}."

PDF_JS = """
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto('file://' + process.argv[1]);
  await p.pdf({ path: process.argv[2], width: '420mm', height: '594mm', printBackground: true });
  await b.close();
})();
"""


def print_pdf() -> str:
    try:
        from playwright.sync_api import sync_playwright  # optional, not in requirements
    except ImportError:
        sync_playwright = None
    if sync_playwright is not None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(OUT_HTML.as_uri())
            page.pdf(path=str(OUT_PDF), width="420mm", height="594mm", print_background=True)
            browser.close()
        return "printed with Python Playwright"
    if shutil.which("node"):
        proc = subprocess.run(["node", "-e", PDF_JS, str(OUT_HTML), str(OUT_PDF)], capture_output=True, text=True)
        if proc.returncode == 0:
            return "printed with Node Playwright"
    return ("NOT printed: Playwright not found. Either `pip install playwright && playwright install chromium`, "
            "or open report/poster.html in Chrome and print to PDF (420 x 594 mm, background graphics on).")


def main() -> int:
    OUT_HTML.write_text(build_html(), encoding="utf-8")
    print(f"Wrote {OUT_HTML}")
    print(f"PDF: {print_pdf()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
