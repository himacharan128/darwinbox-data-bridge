"""Every supported input shape, read into the same record structure.

The point of five formats is not breadth for its own sake: a consultant has whatever
the client's old system emitted, and asking them to convert it to CSV first is the
kind of friction this product exists to remove.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from dbx_extraction import Kind, confidence_for, read, read_pasted, sniff

ROOT = Path(__file__).resolve().parents[2]
RUN1 = ROOT / "tests" / "fixtures" / "run1"


def test_detects_what_a_file_actually_is_not_what_it_claims(tmp_path):
    disguised = tmp_path / "employees.csv"          # named .csv, actually a PDF
    disguised.write_bytes((RUN1 / "roster_export.pdf").read_bytes())
    assert sniff(disguised).kind is Kind.PDF


def test_reads_json_with_nested_paths(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"employees": [
        {"id": "EMP-90001", "name": {"first": "Ada", "last": "Lovelace"},
         "contact": {"email": "ada@example.com"}},
    ]}))
    records = read(path)
    assert len(records) == 1
    assert records[0].values["name.first"] == "Ada"
    assert records[0].values["contact.email"] == "ada@example.com"
    assert records[0].source.path.startswith("$.employees")


def test_reads_yaml(tmp_path):
    path = tmp_path / "export.yaml"
    path.write_text(yaml.dump({"records": [{"id": "EMP-90002", "dept": "ENG"}]}))
    records = read(path)
    assert records[0].values["id"] == "EMP-90002"


def test_reads_pasted_text_with_either_delimiter():
    tabbed = read_pasted("Emp ID\tFirst Name\nEMP-90003\tGrace")
    assert tabbed[0].values["First Name"] == "Grace"
    comma = read_pasted("Emp ID,First Name\nEMP-90004,Alan")
    assert comma[0].values["First Name"] == "Alan"
    assert comma[0].source.file == "pasted text"


def test_reads_a_native_pdf_table():
    records = read(RUN1 / "roster_export.pdf")
    assert len(records) == 3
    assert records[0].values["Emp ID"] == "EMP-00040"
    assert records[0].values["Designation"] == "Staff Engineer"
    assert records[0].source.page == 1


def test_reads_a_scanned_pdf_through_ocr():
    """No text layer at all — every value comes from recognising the image."""
    import pymupdf

    doc = pymupdf.open(RUN1 / "scanned_roster.pdf")
    assert doc[0].get_text().strip() == "", "the fixture must be a real scan"
    doc.close()

    records = read(RUN1 / "scanned_roster.pdf")
    assert len(records) == 3
    assert records[0].values["Emp ID"] == "EMP-00040"


def test_ocr_reports_confidence_per_value():
    records = read(RUN1 / "scanned_roster.pdf")
    cells = confidence_for(records[0].id)
    assert cells, "OCR must report how sure it was, value by value"
    assert any(c.confidence < 0.80 for c in cells.values())
    assert all(c.bbox is not None for c in cells.values()), "and where it read it"


def test_excel_multi_sheet_and_typed_cells():
    records = read(RUN1 / "payroll_staff.xlsx")
    assert records[0].source.sheet == "Staff"
    # Excel hands back datetimes; rendering them with str() would invent a format
    # the source never had.
    assert records[0].values["birth_date"] == "1985-03-14"


@pytest.mark.parametrize("name", ["hrms_employees_export.csv", "payroll_staff.xlsx",
                                  "contractors_2024.csv", "workday_extract.csv",
                                  "legacy_hrms_dump.csv", "roster_export.pdf",
                                  "scanned_roster.pdf"])
def test_every_fixture_reads_with_provenance(name):
    records = read(RUN1 / name)
    assert records
    for record in records:
        assert record.source.file == name
        assert record.source.label()
