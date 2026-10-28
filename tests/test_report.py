"""Phase 4 report/poster generators (skipped if the optional report deps are missing)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("docx")
pytest.importorskip("matplotlib")

REPORT = Path(__file__).resolve().parent.parent / "report"
sys.path.insert(0, str(REPORT))

import facts  # noqa: E402


def test_placeholders_come_from_the_code():
    names = [n for n, _, _ in facts.placeholders()]
    for needed in ("Sensor tolerance", "Attribution coefficients", "Intensity uncertainty", "Forecast error model",
                   "Tier thresholds", "p (credits per g CO2e)", "rho (rebate share)", "gamma (rebate cap / fee)",
                   "k (dead-zone width)", "Decay half-life"):
        assert needed in names


def test_mock_e1_is_never_used_as_evidence(tmp_path, monkeypatch):
    (tmp_path / "e1.json").write_text('{"mock": true, "summary": []}')
    monkeypatch.setattr(facts, "RESULTS", tmp_path)
    assert facts.e1() is None
    assert "PENDING" in facts.headlines()["e1"]


@pytest.mark.skipif(not (facts.RESULTS / "e3.json").exists() or not (facts.FIGURES / "captions.json").exists(),
                    reason="run experiments/run_offline.py and report/make_figures.py first")
def test_report_has_real_headings_and_marks_pending():
    import make_report
    doc = make_report.build()
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    for h in ("Abstract", "1. Problem", "2. Related work (short)", "3. Design", "5. Security model",
              "6. Experiments and results", "7. Limitations", "8. Placeholder parameters", "9. Conclusion"):
        assert h in headings
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "[UNVERIFIED]" in text and facts.NOVELTY in text
    if facts.e1() is None:
        assert "PENDING (run on the laptop)" in text
    assert 'TOC \\o' in doc.element.body.xml  # table of contents field
    assert 'PAGE' in doc.sections[0].footer._element.xml  # page numbers
