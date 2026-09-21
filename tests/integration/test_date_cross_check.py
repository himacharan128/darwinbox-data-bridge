"""An undecidable date column, measured against the same people in other files.

Nothing inside '05/07/2018' says which way round it is. Another export holding the
same person's 2018-07-05 does. That is evidence, so it earns a recommendation - and
never the answer, which stays with a person.
"""
from __future__ import annotations

from dbx_contracts import ExtractedRecord, MigrationSchema, SourceRef
from dbx_migration_core import Pipeline

SCHEMA = MigrationSchema.model_validate({
    "entity": "person",
    "fields": [
        {"name": "person_id", "type": "string", "required": True, "unique": True},
        {"name": "email", "type": "email", "required": True, "unique": True},
        {"name": "joined", "type": "date"},
    ],
})

PEOPLE = [("P-001", "a@x.example"), ("P-002", "b@x.example"), ("P-003", "c@x.example")]


def _rows(file: str, joined: list[str], people=PEOPLE) -> list[ExtractedRecord]:
    return [
        ExtractedRecord(
            id=f"{file}-{i}", source=SourceRef(file=file, row=i),
            values={"person_id": pid, "email": mail, "joined": when},
        )
        for i, ((pid, mail), when) in enumerate(zip(people, joined, strict=True), start=1)
    ]


def _date_case(ambiguous: list[str], iso: list[str], people=PEOPLE):
    result = Pipeline(run_id="t", schema=SCHEMA).run(
        {"iso.csv": _rows("iso.csv", iso), "slash.csv": _rows("slash.csv", ambiguous, people)},
        lookups={},
    )
    case = next(c for c in result.cases if c.headline.startswith("How should dates"))
    return case, result


def test_every_overlapping_person_agreeing_recommends_that_reading():
    case, result = _date_case(
        ["05/07/2018", "02/09/2019", "08/11/2021"],
        ["2018-07-05", "2019-09-02", "2021-11-08"],
    )
    cross = case.evidence["cross_check"]
    assert cross["checked"] == 3 and cross["agree"] == {"Day first": 3, "Month first": 0}

    day, month = (next(o for o in case.options if o.value == f) for f in ("%d/%m/%Y", "%m/%d/%Y"))
    assert day.recommended and not day.caution
    assert not month.recommended and "None of the 3" in (month.caution or "")
    assert "3 of 3 match day first" in case.detail
    # Still a question: the evidence recommends, a person decides.
    assert case.state.value == "open"
    assert any(e.action == "format.cross_checked" for e in result.audit)


def test_files_that_split_are_reported_but_recommend_nothing():
    case, _ = _date_case(
        ["05/07/2018", "02/09/2019", "08/11/2021"],
        ["2018-07-05", "2019-02-09", "2021-11-08"],   # the second reads month first
    )
    assert case.evidence["cross_check"]["agree"] == {"Day first": 2, "Month first": 1}
    assert not any(o.recommended for o in case.options)
    assert not any(o.caution for o in case.options)
    assert "does not settle it" in case.detail


def test_nobody_in_common_means_nothing_to_recommend():
    strangers = [("Q-001", "q@x.example"), ("Q-002", "r@x.example"), ("Q-003", "s@x.example")]
    case, _ = _date_case(
        ["05/07/2018", "02/09/2019", "08/11/2021"],
        ["2018-07-05", "2019-09-02", "2021-11-08"],
        people=strangers,
    )
    assert "cross_check" not in case.evidence
    assert not any(o.recommended or o.caution for o in case.options)
