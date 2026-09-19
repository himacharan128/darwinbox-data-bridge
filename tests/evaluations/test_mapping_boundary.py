"""The mapping boundary, measured against the labelled corpus.

Runs entirely from recorded model replays under tests/fixtures/model-cache, so it
needs no credentials and no network. A replay of a real recorded call is not a
hand-written fixture; re-record with scripts/eval_mapping.py when prompts change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from dbx_contracts import LLM_VOTE_CAP, Evidence, Signal, Thresholds

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from eval_mapping import collect, evaluate, load_schema  # noqa: E402


@pytest.fixture(scope="module")
def measured():
    schema = load_schema()
    rows = collect(schema, use_llm=True)
    return schema, rows, evaluate(rows, schema, Thresholds())


def test_never_auto_applies_a_wrong_mapping(measured):
    """The safety property. Holds at every threshold in the sweep, not just this one."""
    _, _, m = measured
    assert m["auto_wrong"] == 0


def test_never_silently_resolves_a_case_that_needs_a_human(measured):
    _, _, m = measured
    assert m["under_escalated"] == 0
    assert m["recall"] == 1.0


def test_most_mappings_are_applied_without_a_human(measured):
    """Criterion 3 is a boundary, not a queue. If everything escalates, nothing works."""
    _, rows, m = measured
    assert m["auto_correct"] / len(rows) >= 0.70


def test_model_vote_cannot_decide_alone():
    """The cap, stated as an invariant rather than a comment.

    A candidate with no deterministic support scores at most LLM_VOTE_CAP, which sits
    below the review floor — so a confident model cannot carry a mapping by itself.
    """
    certain = Evidence(signals={Signal.LLM_VOTE: 1.0})
    assert certain.deterministic_score == 0.0
    assert certain.score <= LLM_VOTE_CAP
    assert certain.score < Thresholds().review_floor
    assert certain.score < Thresholds().auto_apply


def test_model_vote_share_is_bounded_even_with_thin_evidence():
    thin = Evidence(signals={Signal.NAME_SIM: 0.05, Signal.LLM_VOTE: 1.0})
    assert thin.llm_share <= LLM_VOTE_CAP + 1e-9


def test_ambiguous_column_is_held_back(measured):
    """ESC-001: 'contact' splits 3 emails / 3 phones and must never auto-apply."""
    _, _, m = measured
    contact = [d for d in m["details"] if d[0].column == "contact"]
    assert contact, "the ambiguous column vanished from the corpus"
    _, mapping, auto, _ = contact[0]
    assert not auto
    assert mapping.chosen_field is None


def test_thresholds_are_calibrated_not_asserted():
    t = Thresholds()
    assert t.calibrated is True
    assert "eval_mapping" in t.calibration_ref
