"""Eval runners: the deterministic gate passes; the LLM pack is opt-in."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _cases() -> list[dict]:
    return [
        json.loads(line)
        for line in (ROOT / "evals" / "golden.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_golden_set_shape():
    cases = _cases()
    assert len(cases) == 50
    kinds = {c["kind"] for c in cases}
    assert kinds == {"clause", "document", "round_diff", "round_chain", "exhibit"}
    positions = {c["expected_position"] for c in cases if c["kind"] == "clause"}
    assert positions == {"preferred", "acceptable_fallback", "review_required", "walk_away"}
    postures = {c["expected_posture"] for c in cases if c["kind"] == "document"}
    assert postures == {"aligned", "negotiable", "escalation_required"}


def test_deterministic_runner_scores_full_marks():
    sys.path.insert(0, str(ROOT / "evals"))
    import run_evals

    scored = run_evals.evaluate(_cases())
    assert scored["correct"] == scored["total"] == 50
    assert scored["failures"] == []


def test_llm_pack_refuses_to_run_without_a_provider():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "evals" / "run_llm_evals.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(ROOT)},
    )
    assert proc.returncode == 2
    assert "NO PROVIDER CONFIGURED" in proc.stderr
