"""What each answer to "are these the same person?" does.

Merging two people is the one mistake that cannot be seen afterwards - their data
becomes one record - so it must only ever happen because somebody chose it.
"""
from __future__ import annotations

from dbx_contracts import Action, EscalationClass, Option, ReviewCase
from dbx_migration_core import Overrides, apply_decision


def _case(recommend: str = "separate") -> ReviewCase:
    return ReviewCase(
        id="case-001", run_id="t", klass=EscalationClass.UNCERTAIN_IDENTITY,
        headline="Are these the same person?", detail="same name, same birthday",
        evidence={"keys": ["P-001", "P-002"]},
        actions=[Action.APPROVE, Action.REJECT],
        options=[
            Option(label="Same person — merge", value="merge",
                   recommended=recommend == "merge"),
            Option(label="Different people — keep separate", value="separate",
                   recommended=recommend == "separate"),
        ],
    )


def _merged(action: Action, value: str | None, recommend: str = "separate") -> bool | None:
    return apply_decision(Overrides(), _case(recommend), action, value).merge.get(
        ("P-001", "P-002")
    )


def test_choosing_same_person_merges_them():
    assert _merged(Action.CORRECT, "merge") is True


def test_choosing_different_people_keeps_them_apart():
    assert _merged(Action.CORRECT, "separate") is False


def test_approving_takes_the_recommendation_rather_than_merging():
    assert _merged(Action.APPROVE, None) is False
    assert _merged(Action.APPROVE, None, recommend="merge") is True


def test_an_answer_that_is_neither_decides_nothing():
    assert _merged(Action.CORRECT, "maybe") is None
