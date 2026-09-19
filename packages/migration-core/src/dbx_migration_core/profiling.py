"""Build a bounded profile of a column by streaming every value.

The profile replaces the column everywhere: the model never sees raw data, and the
evidence scorer reads parse rates and constraint fits straight out of it. Built once,
used on both sides of the autonomy boundary.

Cost is ~300 tokens per column regardless of row count, so a 40-column x 500k-row
export costs the same as 40 x 50.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import statistics
from collections import Counter

from dbx_contracts import ColumnProfile, PatternMask, SourceRef, TypeCandidate, ValueCount

#: Values that mean "absent" rather than a real value.
NULL_MARKERS = {"", "-", "--", "n/a", "na", "null", "none", "nil", "nan", "#n/a", "?"}

#: Date formats tried per column, widest-coverage first.
DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%Y%m%d",
)

#: Formats where swapping the first two components yields another valid format.
AMBIGUOUS_PAIRS = {("%d/%m/%Y", "%m/%d/%Y"), ("%d-%m-%Y", "%m-%d-%Y")}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE = re.compile(r"^\+?[0-9][0-9\s\-().]{6,}[0-9]$")
_INT = re.compile(r"^[+-]?\d+$")
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)$")
_BOOL = {"true", "false", "yes", "no", "y", "n", "0", "1", "t", "f"}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

SAMPLE_SIZE = 15
TOP_K = 10


def normalize_tokens(header: str) -> list[str]:
    """Split a header into comparable tokens: 'Date of Joining' -> [date, of, joining]."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", header)
    return [t for t in _TOKEN_SPLIT.split(spaced.lower()) if t]


def is_null_marker(value: str | None) -> bool:
    return value is None or value.strip().lower() in NULL_MARKERS


def pattern_mask(value: str) -> str:
    """Collapse a value to its shape: 'EMP-00012' -> 'A{3}-#{5}'.

    Structured identifiers collapse to a stable mask with high coverage; free text
    does not, and that low coverage is itself the signal.
    """
    classes: list[str] = []
    for ch in value:
        if ch.isdigit():
            classes.append("#")
        elif ch.isalpha():
            classes.append("A" if ch.isupper() else "a")
        elif ch.isspace():
            classes.append("_")
        else:
            classes.append(ch)

    out: list[str] = []
    for cls in classes:
        if out and out[-1][0] == cls:
            out[-1] = (cls, out[-1][1] + 1) if isinstance(out[-1], tuple) else (cls, 2)
        else:
            out.append((cls, 1))
    return "".join(c if n == 1 else f"{c}{{{n}}}" for c, n in out)


def _date_rates(values: list[str]) -> tuple[dict[str, float], dict[str, bool]]:
    """Parse rate per date format, plus whether each format is *decidable*.

    Decidable means some value in the column can only be read one way — a day
    component above 12 under DD/MM, for instance. That is the concrete evidence
    behind "unambiguous date", and its absence is what forces AMBIGUOUS_VALUE.
    """
    rates: dict[str, float] = {}
    decidable: dict[str, bool] = {}
    for fmt in DATE_FORMATS:
        parsed = 0
        distinguishing = False
        for v in values:
            try:
                d = dt.datetime.strptime(v, fmt).replace(tzinfo=dt.UTC)
            except ValueError:
                continue
            parsed += 1
            if d.day > 12:
                distinguishing = True
        if parsed:
            rates[fmt] = parsed / len(values)
            decidable[fmt] = distinguishing
    return rates, decidable


def profile_column(
    source: SourceRef, header: str, raw_values: list[str | None], *, seed: int = 0
) -> ColumnProfile:
    total = len(raw_values)
    markers = sorted(
        {(v or "").strip() for v in raw_values if is_null_marker(v) and (v or "").strip()}
    )
    values = [v.strip() for v in raw_values if not is_null_marker(v)]
    non_null = len(values)

    counts = Counter(values)
    distinct = len(counts)

    type_candidates: list[TypeCandidate] = []
    if non_null:

        def rate(pred) -> float:
            return sum(1 for v in values if pred(v)) / non_null

        for name, pred in (
            ("integer", lambda v: bool(_INT.match(v))),
            ("number", lambda v: bool(_NUM.match(v))),
            ("boolean", lambda v: v.lower() in _BOOL),
            ("email", lambda v: bool(_EMAIL.match(v))),
            ("phone", lambda v: bool(_PHONE.match(v))),
        ):
            r = rate(pred)
            if r > 0:
                type_candidates.append(TypeCandidate(type=name, parse_rate=r))

        date_rates, _decidable = _date_rates(values)
        if date_rates:
            best = max(date_rates.values())
            type_candidates.append(
                TypeCandidate(
                    type="date",
                    parse_rate=best,
                    formats=[
                        f
                        for f, r in sorted(date_rates.items(), key=lambda kv: -kv[1])
                        if r >= best - 1e-9
                    ],
                    format_rates={f: round(r, 4) for f, r in date_rates.items()},
                )
            )

        type_candidates.append(TypeCandidate(type="string", parse_rate=1.0))
        type_candidates.sort(key=lambda c: -c.parse_rate)

    mask_counts = Counter(pattern_mask(v) for v in values)
    masks = (
        [PatternMask(mask=m, coverage=c / non_null) for m, c in mask_counts.most_common(3)]
        if non_null
        else []
    )

    lengths = [len(v) for v in values]
    rng = random.Random(seed)
    sample = _stratified_sample(values, counts, rng)

    return ColumnProfile(
        source=source,
        raw_name=header,
        normalized_tokens=normalize_tokens(header),
        total=total,
        non_null=non_null,
        null_count=total - non_null,
        null_markers_found=markers,
        distinct=distinct,
        cardinality_ratio=(distinct / non_null) if non_null else 0.0,
        type_candidates=type_candidates,
        masks=masks,
        top_values=[ValueCount(value=v, count=c) for v, c in counts.most_common(TOP_K)],
        sample=sample,
        length_min=min(lengths) if lengths else None,
        length_max=max(lengths) if lengths else None,
        length_mean=round(statistics.fmean(lengths), 2) if lengths else None,
    )


def _stratified_sample(values: list[str], counts: Counter, rng: random.Random) -> list[str]:
    """5 most common, 5 random, 5 outliers — outliers are where the surprises live."""
    if not values:
        return []
    common = [v for v, _ in counts.most_common(5)]
    pool = list(counts)
    random_pick = rng.sample(pool, min(5, len(pool)))
    by_len = sorted(pool, key=len)
    rare = [v for v, c in counts.most_common()[:-6:-1]]
    outliers = [by_len[-1], by_len[0], *rare[:3]]

    out: list[str] = []
    for v in [*common, *random_pick, *outliers]:
        if v not in out:
            out.append(v)
        if len(out) >= SAMPLE_SIZE:
            break
    return out


def decidable_date_formats(values: list[str]) -> tuple[dict[str, float], dict[str, bool]]:
    """Public access to the parse/decidability pair, used by the cleanup rules."""
    clean = [v.strip() for v in values if not is_null_marker(v)]
    return _date_rates(clean)
