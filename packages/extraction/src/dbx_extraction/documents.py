"""PDF extraction: native text first, OCR only for pages that need it.

Extraction uncertainty is a different kind from mapping or matching uncertainty and
deserves its own evidence. OCR reports a per-word confidence and a bounding box, so a
case can show the *cropped image* beside the value it read — which a consultant can
settle at a glance in a way that no confidence score allows.

The rule is deliberately two-sided: low confidence alone is not enough (OCR is
routinely unsure about text it got right), and a pattern miss alone is not enough
(the file may simply contain a bad value). Escalation needs both.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import pymupdf as fitz
from dbx_contracts import ExtractedRecord, SourceRef

from .tabular import UnsupportedInput, record_id

#: Below this, a word is worth doubting when something else also looks wrong.
LOW_CONFIDENCE = 0.80
#: A page with less real text than this is almost certainly a scan.
MIN_NATIVE_CHARS = 60


@dataclass
class Word:
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1 in page points


@dataclass
class Cell:
    value: str
    confidence: float = 1.0
    bbox: tuple[float, float, float, float] | None = None
    page: int = 1

    @property
    def uncertain(self) -> bool:
        return self.confidence < LOW_CONFIDENCE


@dataclass
class PageText:
    page: int
    native: bool
    rows: list[list[Cell]] = field(default_factory=list)


class OcrEngine(Protocol):
    def recognize(self, png_bytes: bytes, page: int) -> list[Word]: ...


class RecordedOcr:
    """Replay OCR output recorded from a real engine.

    The same bargain as the model cache: a replay of a real run keeps the demo and the
    tests working with no heavyweight dependency installed, and it is labelled as a
    replay rather than passed off as a live read.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = json.loads(path.read_text()) if path.exists() else {}

    @property
    def available(self) -> bool:
        return bool(self._data)

    def recognize(self, png_bytes: bytes, page: int) -> list[Word]:
        words = self._data.get(str(page), [])
        return [Word(w["text"], w["confidence"], tuple(w["bbox"])) for w in words]


class DocTROcr:
    """Open-source OCR (Apache 2.0) with per-word confidence and boxes."""

    def __init__(self) -> None:
        from doctr.models import ocr_predictor

        self._model = ocr_predictor(pretrained=True)

    def recognize(self, png_bytes: bytes, page: int) -> list[Word]:
        import numpy as np
        from doctr.io import DocumentFile

        doc = DocumentFile.from_images([png_bytes])
        result = self._model(doc)
        out: list[Word] = []
        for block in result.pages[0].blocks:
            for line in block.lines:
                for word in line.words:
                    (x0, y0), (x1, y1) = word.geometry
                    out.append(Word(word.value, float(word.confidence), (x0, y0, x1, y1)))
        _ = np
        return out


def load_ocr(recorded: Path | None = None) -> OcrEngine | None:
    if recorded is not None:
        engine = RecordedOcr(recorded)
        if engine.available:
            return engine
    try:
        return DocTROcr()
    except Exception:  # noqa: BLE001 - absence of the optional engine is normal
        return None


def _rows_from_native(page: fitz.Page, number: int) -> list[list[Cell]]:
    """Recover the table from a page that has a real text layer.

    PyMuPDF's text-strategy table finder aligns columns properly, which hand-rolled
    x-clustering does not: multi-word cells like "Emp ID" and "Staff Engineer" split
    on whitespace and leave every row a different width.
    """
    try:
        found = page.find_tables(strategy="text")
    except Exception:  # noqa: BLE001 - fall back rather than fail the whole read
        found = None

    if found and found.tables:
        table = max(found.tables, key=lambda t: len(t.extract()))
        box = table.bbox
        rows: list[list[Cell]] = []
        data = table.extract()
        height = (box[3] - box[1]) / max(len(data), 1)
        for r, row in enumerate(data):
            cells = []
            width = (box[2] - box[0]) / max(len(row), 1)
            for c, value in enumerate(row):
                text = (value or "").strip()
                cells.append(Cell(
                    text, 1.0,
                    (box[0] + c * width, box[1] + r * height,
                     box[0] + (c + 1) * width, box[1] + (r + 1) * height),
                    number,
                ))
            rows.append(cells)
        return rows

    # No table structure: fall back to grouping words into visual rows.
    words = page.get_text("words")
    buckets: dict[int, list[tuple[float, str, tuple]]] = {}
    for x0, y0, x1, y1, text, *_ in words:
        if text.strip():
            buckets.setdefault(round(y0 / 6), []).append((x0, text, (x0, y0, x1, y1)))
    return [
        [Cell(t, 1.0, b, number) for _, t, b in sorted(buckets[k])]
        for k in sorted(buckets)
    ]


def _rows_from_words(words: list[Word], number: int) -> list[list[Cell]]:
    """Group OCR words into lines by vertical position.

    Rounding y to a fixed grid splits a line whenever glyph heights differ — a capital
    and a comma on the same line sit at different tops. Cluster on the gap between
    consecutive vertical centres instead, scaled to the typical word height so it
    works whatever the page size.
    """
    if not words:
        return []

    heights = sorted((w.bbox[3] - w.bbox[1]) for w in words)
    typical = heights[len(heights) // 2] or 0.01
    ordered = sorted(words, key=lambda w: (w.bbox[1] + w.bbox[3]) / 2)

    lines: list[list[Word]] = [[ordered[0]]]
    for word in ordered[1:]:
        centre = (word.bbox[1] + word.bbox[3]) / 2
        previous = lines[-1][-1]
        last_centre = (previous.bbox[1] + previous.bbox[3]) / 2
        if centre - last_centre > typical * 0.7:
            lines.append([])
        lines[-1].append(word)

    return [
        [Cell(w.text, w.confidence, w.bbox, number)
         for w in sorted(line, key=lambda w: w.bbox[0])]
        for line in lines
    ]


def _gap_threshold(gaps: list[float]) -> float:
    """Find the size that separates a word-space from a column boundary.

    Both kinds of gap appear in every row, and they are different sizes, so the
    boundary shows up as the largest proportional jump in the sorted gaps. Scaling by
    word width instead fails badly here: an email is twenty times wider than a
    department code, so any width-derived threshold swallows whole rows.
    """
    ordered = sorted(g for g in gaps if g > 0)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0] * 0.5

    # Take the FIRST clear separation, not the biggest one. The largest ratio is
    # often between two column gaps at opposite ends of a wide row, which would
    # swallow the whole line into one cell.
    for lo, hi in pairwise(ordered):
        if lo > 0 and hi / lo >= 1.8:
            return hi * 0.9
    return ordered[0] * 0.5



def _split_on_gaps(row: list[Cell]) -> list[Cell]:
    """Join words separated by a word-space, split where a column gap appears.

    OCR emits words, not cells. "Emp ID" is two words and "EMP-00040" is one, so any
    rule that treats a word as a cell gives every row a different width. Within a row
    the two gaps are clearly different sizes — a space versus a column boundary — so
    split on the larger and join across the smaller.
    """
    cells = [c for c in row if c.bbox and c.value.strip()]
    if len(cells) < 2:
        return cells
    cells.sort(key=lambda c: c.bbox[0])

    gaps = [cells[i + 1].bbox[0] - cells[i].bbox[2] for i in range(len(cells) - 1)]
    threshold = _gap_threshold(gaps)

    groups: list[list[Cell]] = [[cells[0]]]
    for previous, current in pairwise(cells):
        if current.bbox[0] - previous.bbox[2] > threshold:
            groups.append([])
        groups[-1].append(current)

    merged: list[Cell] = []
    for group in groups:
        boxes = [c.bbox for c in group]
        merged.append(Cell(
            " ".join(c.value for c in group).strip(),
            min(c.confidence for c in group),
            (min(b[0] for b in boxes), min(b[1] for b in boxes),
             max(b[2] for b in boxes), max(b[3] for b in boxes)),
            group[0].page,
        ))
    return merged


def tabulate(rows: list[list[Cell]], *, tolerance: float | None = None) -> list[list[Cell]]:
    """Recover a table from loose OCR words.

    Data rows split cleanly on gap size — one value per column, with the occasional
    two-word value. Header rows do not: "Date of Joining" and "Employment Type" have
    the same internal spacing as the gap between short columns, so splitting them by
    gap alone merges or fragments them unpredictably.

    So the grid comes from the data rows, and every row's raw words are then snapped
    onto it. A header word lands in whichever column it sits above, which is what a
    reader does.
    """
    _ = tolerance
    if not rows:
        return []

    split = [r for r in (_split_on_gaps(row) for row in rows) if r]
    if not split:
        return []

    from collections import Counter

    widths = Counter(len(row) for row in split)
    grid_width = max(widths, key=lambda w: (widths[w], w))
    reference = [row for row in split if len(row) == grid_width]
    if not reference:
        return split

    # Column boundaries sit midway between the left edges of neighbouring columns.
    lefts = [
        sum(row[i].bbox[0] for row in reference) / len(reference)
        for i in range(grid_width)
    ]
    rights = [
        sum(row[i].bbox[2] for row in reference) / len(reference)
        for i in range(grid_width)
    ]
    bounds = [(lefts[i] + rights[i]) / 2 for i in range(grid_width)]

    out: list[list[Cell]] = []
    for row in rows:
        words = sorted(
            (c for c in row if c.bbox and c.value.strip()), key=lambda c: c.bbox[0]
        )
        buckets: list[list[Cell]] = [[] for _ in bounds]
        for word in words:
            centre = (word.bbox[0] + word.bbox[2]) / 2
            index = min(range(len(bounds)), key=lambda i: abs(bounds[i] - centre))
            buckets[index].append(word)
        line: list[Cell] = []
        for bucket in buckets:
            if not bucket:
                line.append(Cell("", 1.0, None, row[0].page if row else 1))
                continue
            boxes = [c.bbox for c in bucket]
            line.append(Cell(
                " ".join(c.value for c in bucket).strip(),
                min(c.confidence for c in bucket),
                (min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)),
                bucket[0].page,
            ))
        out.append(line)
    return out


def read_pages(path: Path, ocr: OcrEngine | None = None) -> list[PageText]:
    """Native text where it exists, OCR only where it does not."""
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise UnsupportedInput(f"{path.name}: PDF could not be opened ({exc})") from exc

    pages: list[PageText] = []
    for number, page in enumerate(doc, start=1):
        text = page.get_text().strip()
        if len(text) >= MIN_NATIVE_CHARS:
            pages.append(PageText(number, True, _rows_from_native(page, number)))
            continue
        if ocr is None:
            pages.append(PageText(number, False, []))
            continue
        png = page.get_pixmap(dpi=200).tobytes("png")
        pages.append(PageText(number, False, _rows_from_words(ocr.recognize(png, number), number)))
    doc.close()
    return pages


def crop(path: Path, page: int, bbox: tuple[float, float, float, float], *, pad: float = 6.0,
         normalized: bool = False) -> bytes:
    """The picture of the thing the agent could not read, for the review card."""
    doc = fitz.open(path)
    target = doc[page - 1]
    if normalized:
        width, height = target.rect.width, target.rect.height
        bbox = (bbox[0] * width, bbox[1] * height, bbox[2] * width, bbox[3] * height)
    rect = fitz.Rect(*bbox) + (-pad, -pad, pad, pad)
    png = target.get_pixmap(dpi=200, clip=rect & target.rect).tobytes("png")
    doc.close()
    return png


def read_pdf(
    path: Path, *, display_name: str | None = None, ocr: OcrEngine | None = None,
    recorded_ocr: Path | None = None,
) -> list[ExtractedRecord]:
    """Read a PDF whose first usable row is a header and the rest are records."""
    name = display_name or path.name
    engine = ocr or load_ocr(recorded_ocr or path.with_suffix(".ocr.json"))
    pages = read_pages(path, engine)

    rows: list[tuple[int, list[Cell]]] = []
    for page_text in pages:
        # A table finder pads every row to the table width, so blank rows arrive full
        # of empty cells. Counting them would make the modal row width zero.
        usable = [
            row for row in page_text.rows if sum(1 for c in row if c.value.strip()) > 1
        ]
        if not usable:
            continue
        # Native rows arrive already aligned; OCR gives loose words that need snapping.
        gridded = usable if page_text.native else tabulate(usable, tolerance=0.035)
        rows.extend((page_text.page, row) for row in gridded)
    if not rows:
        raise UnsupportedInput(
            f"{name}: no readable text. If this is a scan, OCR is unavailable."
        )

    # The header is the first row as wide as the table, not the first row on the page.
    # Titles, page numbers and footers are narrow rows and would otherwise be mistaken
    # for column names.
    from collections import Counter

    filled = Counter(sum(1 for c in cells if c.value.strip()) for _, cells in rows)
    table_width = max(filled, key=lambda w: (filled[w], w))
    header_index = next(
        (i for i, (_, cells) in enumerate(rows)
         if sum(1 for c in cells if c.value.strip()) == table_width),
        0,
    )
    header_cells = rows[header_index][1]
    headers = [c.value.strip() or f"column_{i + 1}" for i, c in enumerate(header_cells)]
    body = rows[header_index + 1 :]

    out: list[ExtractedRecord] = []
    for index, (page_no, cells) in enumerate(body, start=1):
        if sum(1 for c in cells if c.value.strip()) < max(2, table_width // 2):
            continue  # a title, a page number or a footer, not a record
        values: dict[str, str | None] = {}
        confidences: dict[str, Cell] = {}
        for i, header in enumerate(headers):
            cell = cells[i] if i < len(cells) else None
            values[header] = (cell.value or None) if cell else None
            if cell:
                confidences[header] = cell
        record = ExtractedRecord(
            id=record_id(name, None, index),
            source=SourceRef(file=name, page=page_no, row=index),
            values=values,
        )
        out.append(record)
        _CONFIDENCE[record.id] = confidences
    return out


#: Per-cell OCR confidence for records read from a document, keyed by record id.
_CONFIDENCE: dict[str, dict[str, Cell]] = {}


def confidence_for(record_id_: str) -> dict[str, Cell]:
    return _CONFIDENCE.get(record_id_, {})
