"""The fixtures must actually exhibit what the corpus claims about them.

A corpus that says a column is ambiguous, over a column that is not, produces a
measurement that means nothing. These tests keep the labels honest: edit a fixture
and break a claim, and the build says so.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

ESCALATION_CLASSES = {
    "AMBIGUOUS_MAPPING",
    "UNMAPPED_REQUIRED",
    "AMBIGUOUS_VALUE",
    "UNCERTAIN_IDENTITY",
    "CONFLICTING_FACTS",
    "MISSING_REQUIRED",
    "VALIDATION_UNRESOLVED",
    "UNRESOLVED_REFERENCE",
    "LOW_CONFIDENCE_EXTRACTION",
    "CROSS_RUN_COLLISION",
    "DELIVERY_PERMANENT_FAILURE",
}

DMY = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def _day_month(rows, col):
    out = []
    for r in rows:
        m = DMY.match(str(r.get(col) or "").strip())
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


# ---------- the target schema must be adversarial, not derived from the sources ----------


def test_identifier_is_case_sensitive_and_patterned(fields):
    emp = fields["employee_id"]
    assert emp["case_sensitive"] is True
    assert emp["pattern"] == r"^EMP-[0-9]{5}$"
    assert emp["unique"] is True


def test_required_field_has_no_source_anywhere(fields, hrms, payroll, contractors):
    """status is required by the target and supplied by nothing (ESC-002)."""
    assert fields["status"]["required"] is True
    columns = set(hrms[0]) | set(payroll[0]) | set(contractors[0])
    assert not [c for c in columns if "status" in str(c).lower()]


def test_most_fields_must_map_without_an_alias(fields):
    """Alias independence: if most fields had aliases the engine would be a lookup table."""
    without = [n for n, f in fields.items() if not f.get("aliases")]
    assert len(without) >= 8, f"only {len(without)} fields lack aliases"


# ---------- the date claim: SAFE-006 auto-resolves, ESC-003 cannot ----------


@pytest.mark.parametrize("column", ["DOB", "Date of Joining"])
def test_hrms_date_columns_have_a_disambiguator(hrms, column):
    """A day>12 somewhere in the column establishes DD/MM for the whole column."""
    pairs = _day_month(hrms, column)
    assert pairs, f"no DD/MM/YYYY values parsed from {column}"
    assert any(d > 12 for d, _ in pairs)


def test_payroll_joining_column_is_genuinely_ambiguous(payroll):
    """ESC-003: DD/MM and MM/DD both parse every value, so neither can be chosen."""
    pairs = _day_month(payroll, "joining_dt")
    assert pairs
    assert all(d <= 12 and m <= 12 for d, m in pairs), (
        "a value with day>12 would resolve the column and destroy the case"
    )


# ---------- the ambiguous mapping column ----------


def test_contact_column_has_no_dominant_type(contractors):
    """ESC-001: a lopsided split would let work_email win on type-parse rate."""
    values = [r["contact"] for r in contractors]
    emails = sum(1 for v in values if "@" in v)
    assert abs(emails - (len(values) - emails)) <= 1


# ---------- record-level escalation instances ----------


def test_missing_required_value_exists(by_id):
    assert by_id["EMP-00011"]["DOB"] == ""


def test_email_that_survives_safe_cleanup_still_fails(by_id):
    """ESC-007: no TLD. Trimming and domain-casing cannot repair it."""
    local, _, domain = by_id["EMP-00021"]["Email"].partition("@")
    assert local and domain and "." not in domain


def test_unresolvable_department_reference(by_id, departments):
    assert by_id["EMP-00008"]["Dept Code"] not in {d["code"] for d in departments}


def test_manager_self_reference(by_id):
    assert by_id["EMP-00026"]["Manager ID"] == "EMP-00026"


def test_conflicting_designation_across_files(by_id, payroll):
    payroll_by_id = {r["staff_code"]: r for r in payroll}
    assert by_id["EMP-00004"]["Designation"] != payroll_by_id["EMP-00004"]["grade"]


def test_namesake_with_different_identifier(by_id, contractors):
    """ESC-004: same name and DOB, different id — must not auto-merge."""
    twin = next(r for r in contractors if r["code"] == "EMP-00020")
    assert twin["given_name"] == by_id["EMP-00001"]["First Name"]
    assert twin["surname"] == by_id["EMP-00001"]["Last Name"]
    assert twin["dob"] == "1985-03-14"
    assert twin["code"] != "EMP-00001"


def test_cross_run_collision_seed(by_id, run2):
    repeated = [r for r in run2 if r["Emp ID"] in by_id]
    assert repeated, "run2 must repeat at least one run1 employee"
    assert repeated[0]["Designation"] != by_id[repeated[0]["Emp ID"]]["Designation"]


def test_prompt_injection_payload_present(contractors):
    assert any("ignore all prior instructions" in r["role"].lower() for r in contractors)


# ---------- safe-cleanup instances (these must NOT escalate) ----------


def test_whitespace_padding_present(by_id):
    row = by_id["EMP-00002"]
    assert row["First Name"] != row["First Name"].strip()
    assert row["Last Name"] != row["Last Name"].strip()


def test_mixed_case_email_present(by_id):
    email = by_id["EMP-00003"]["Email"]
    assert email != email.lower()
    assert email.split("@")[0].isupper() or any(c.isupper() for c in email.split("@")[0])


def test_enum_variants_present(hrms):
    assert {r["Employment Type"] for r in hrms} >= {"Full Time", "full-time", "FTE", "Intern"}


def test_null_marker_present(by_id):
    assert by_id["EMP-00006"]["Mobile"] == "N/A"


def test_exactly_one_exact_duplicate(hrms):
    dupes = [k for k, n in Counter(r["Emp ID"] for r in hrms).items() if n > 1]
    assert dupes == ["EMP-00010"]


# ---------- corpus completeness ----------


def test_every_escalation_class_has_a_labelled_instance(corpus):
    covered = {c["class"] for c in corpus["escalation_cases"]["cases"]}
    assert ESCALATION_CLASSES <= covered, f"unlabelled: {ESCALATION_CLASSES - covered}"


def test_corpus_ids_are_unique(corpus):
    for name, doc in corpus.items():
        entries = doc.get("cases") or doc.get("safe") or doc.get("forbidden") or []
        ids = [e["id"] for e in entries]
        assert len(ids) == len(set(ids)), f"duplicate ids in {name}"


def test_mapping_corpus_is_mostly_auto(corpus):
    """Criterion 3 is a boundary, not a queue: the corpus must reflect that."""
    cases = corpus["mapping_decisions"]["cases"]
    auto = sum(1 for c in cases if c["expected_outcome"] == "auto")
    # 0.75 after six cases were honestly relabelled to escalate: their target fields
    # declare no discriminating constraint, so only the capped model vote could decide.
    assert auto / len(cases) > 0.75, "too many mapping cases expect escalation"
