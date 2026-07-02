"""
profile.py — Generic data profiler. The bedrock of any-data reporting.

Hand it any tabular data (list of dict rows) and it works out, column by column,
what each column *is* — number, date, category, or free text — and computes the
stats that make sense for that type. It never needs to know what the data means;
it describes the data's shape competently and safely.

Everything above this (generic report rendering, AI narrative, routing) stands on
this profile. So this module is deliberately dependency-free, defensive, and
fully deterministic: same data in, same profile out, never raises on weird input.

Design rules:
  - Never crash on bad data. Unparseable -> treated as text. Empty -> reported empty.
  - Don't guess wildly. A column is only 'number'/'date' if the strong majority of
    its non-blank values parse that way; otherwise it's category or text.
  - Distinguish 'category' (few distinct values, repeated) from 'text' (mostly
    unique, like names/notes) — they report very differently.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from datetime import datetime, date
from collections import Counter

# fraction of non-blank values that must parse as a type to call the column that type
_TYPE_THRESHOLD = 0.80
# at/below this many distinct values (and repeating) => category, else text
_CATEGORY_MAX_DISTINCT = 25
_CATEGORY_MAX_UNIQUE_RATIO = 0.5   # if >50% of values are unique, it's text not category

_NUM_CLEAN_RE = re.compile(r"[,$€£%\s]")
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y",
    "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%b %d, %Y",
    "%d %b %Y", "%Y-%m", "%B %d, %Y",
)

COL_NUMBER = "number"
COL_DATE = "date"
COL_CATEGORY = "category"
COL_TEXT = "text"
COL_EMPTY = "empty"


# --------------------------------------------------------------------------- #
# Value parsers — all return None on failure, never raise
# --------------------------------------------------------------------------- #
def _is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def try_number(v):
    """Parse a number out of messy input ('$1,200', '3.5%', '1.2K'). None if not."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if not (isinstance(v, float) and math.isnan(v)) else None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    mult = 1.0
    low = s.lower()
    if low.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif low.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    cleaned = _NUM_CLEAN_RE.sub("", s)
    if cleaned in ("", "-", ".", "-."):
        return None
    # parens => negative accounting style, e.g. (1,200)
    neg = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        n = float(cleaned) * mult
        return -n if neg else n
    except ValueError:
        return None


def try_date(v):
    """Parse a date. None if not a date."""
    if isinstance(v, (datetime, date)):
        return v if isinstance(v, datetime) else datetime(v.year, v.month, v.day)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) < 6:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Column + dataset profiles
# --------------------------------------------------------------------------- #
@dataclass
class ColumnProfile:
    name: str
    kind: str                      # one of COL_*
    count: int = 0                 # non-blank values
    blank: int = 0
    # numeric
    total: float | None = None
    mean: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    # category
    distinct: int = 0
    top_values: list = field(default_factory=list)   # [(value, count), ...]
    # date
    date_min: str | None = None
    date_max: str | None = None
    # sample for text
    sample: list = field(default_factory=list)


@dataclass
class DataProfile:
    n_rows: int
    n_cols: int
    columns: list[ColumnProfile]
    date_column: str | None = None         # best date column for trends
    numeric_columns: list[str] = field(default_factory=list)
    category_columns: list[str] = field(default_factory=list)

    def col(self, name: str) -> ColumnProfile | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


# --------------------------------------------------------------------------- #
# Profiling
# --------------------------------------------------------------------------- #
def _classify_column(name: str, values: list) -> ColumnProfile:
    non_blank = [v for v in values if not _is_blank(v)]
    blank = len(values) - len(non_blank)
    cp = ColumnProfile(name=name, kind=COL_EMPTY, count=len(non_blank), blank=blank)
    if not non_blank:
        return cp

    n = len(non_blank)
    nums = [try_number(v) for v in non_blank]
    num_ok = [x for x in nums if x is not None]
    dates = [try_date(v) for v in non_blank]
    date_ok = [d for d in dates if d is not None]

    num_frac = len(num_ok) / n
    date_frac = len(date_ok) / n

    # dates take priority over numbers when both plausible (e.g. years vs dates)
    if date_frac >= _TYPE_THRESHOLD and date_frac >= num_frac:
        cp.kind = COL_DATE
        cp.date_min = min(date_ok).date().isoformat()
        cp.date_max = max(date_ok).date().isoformat()
        return cp

    if num_frac >= _TYPE_THRESHOLD:
        cp.kind = COL_NUMBER
        cp.total = sum(num_ok)
        cp.mean = cp.total / len(num_ok)
        cp.minimum = min(num_ok)
        cp.maximum = max(num_ok)
        return cp

    # otherwise it's categorical or free text
    counter = Counter(str(v).strip() for v in non_blank)
    distinct = len(counter)
    unique_ratio = distinct / n
    cp.distinct = distinct
    cp.top_values = counter.most_common(10)

    # Category vs text. A CATEGORY repeats into a small bounded set of labels;
    # free TEXT is mostly one-off (names, IDs, comments). Two ways to qualify as
    # a category, so small datasets don't misfire:
    #   (a) few distinct AND they repeat (low unique ratio) — the normal case
    #   (b) very few distinct in absolute terms (<=8) AND something repeats —
    #       catches small datasets where the ratio only looks high due to few rows
    #       (e.g. 5 age groups across 7 rows).
    # A column where every value is distinct is always text, regardless of count.
    repeats = distinct < n
    small_distinct = distinct <= 8
    bounded = distinct <= _CATEGORY_MAX_DISTINCT and unique_ratio <= _CATEGORY_MAX_UNIQUE_RATIO
    if repeats and (bounded or small_distinct):
        cp.kind = COL_CATEGORY
    else:
        cp.kind = COL_TEXT
        cp.sample = [str(v).strip() for v in non_blank[:5]]
    return cp


def profile_data(rows: list[dict]) -> DataProfile:
    """
    Profile a list of dict rows (e.g. from csv.DictReader). Returns a DataProfile
    describing every column. Never raises on malformed data.
    """
    if not rows:
        return DataProfile(n_rows=0, n_cols=0, columns=[])

    # union of all keys, preserving first-seen order
    col_names = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen and k is not None:
                seen.add(k)
                col_names.append(k)

    columns = []
    for name in col_names:
        values = [r.get(name) for r in rows]
        columns.append(_classify_column(name, values))

    numeric = [c.name for c in columns if c.kind == COL_NUMBER]
    category = [c.name for c in columns if c.kind == COL_CATEGORY]
    dates = [c for c in columns if c.kind == COL_DATE]
    # best date column = the one with the widest span / most coverage
    date_col = None
    if dates:
        date_col = max(dates, key=lambda c: c.count).name

    return DataProfile(
        n_rows=len(rows),
        n_cols=len(columns),
        columns=columns,
        date_column=date_col,
        numeric_columns=numeric,
        category_columns=category,
    )


def load_csv_rows(path: str) -> list[dict]:
    """Read a CSV into dict rows, defensively (handles BOM, blank lines)."""
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f)]
