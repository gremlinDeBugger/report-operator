"""
Tests for the generic data profiler — the foundation of any-data reporting.
These lock in correct column classification across varied, messy, and edge-case
data so nothing built on top inherits a shaky base.
"""
import math
import pytest
from engine.profile import (
    profile_data, try_number, try_date,
    COL_NUMBER, COL_DATE, COL_CATEGORY, COL_TEXT, COL_EMPTY,
)


# ---- value parsers ---- #
@pytest.mark.parametrize("raw,expected", [
    ("$1,200", 1200.0), ("3.5%", 3.5), ("1.2K", 1200.0), ("2M", 2_000_000.0),
    ("(450)", -450.0), ("  88  ", 88.0), ("1,234.56", 1234.56),
    (42, 42.0), (3.14, 3.14),
    ("", None), ("abc", None), ("--", None), (None, None), (True, None),
])
def test_try_number(raw, expected):
    assert try_number(raw) == expected


@pytest.mark.parametrize("raw,ok", [
    ("2026-01-05", True), ("01/05/2026", True), ("Jan 05, 2026", True),
    ("2026-06-01 08:00:00", True), ("not a date", False), ("", False), ("42", False),
])
def test_try_date(raw, ok):
    assert (try_date(raw) is not None) == ok


# ---- column classification ---- #
def test_sales_data_classifies_correctly():
    rows = [
        {"Date": "2026-01-05", "Region": "NE", "Units": "120", "Revenue": "$3,600"},
        {"Date": "2026-01-12", "Region": "NE", "Units": "140", "Revenue": "$4,200"},
        {"Date": "2026-01-19", "Region": "SW", "Units": "95", "Revenue": "$3,325"},
        {"Date": "2026-01-26", "Region": "MW", "Units": "75", "Revenue": "$2,250"},
    ]
    p = profile_data(rows)
    assert p.col("Date").kind == COL_DATE
    assert p.col("Region").kind == COL_CATEGORY
    assert p.col("Units").kind == COL_NUMBER
    assert p.col("Revenue").kind == COL_NUMBER
    assert p.col("Revenue").total == pytest.approx(13375.0)
    assert p.date_column == "Date"


def test_unique_id_is_text_not_category():
    rows = [{"id": f"R{i:03d}", "grade": g} for i, g in
            enumerate(["A", "B", "A", "B", "A", "C", "A"])]
    p = profile_data(rows)
    assert p.col("id").kind == COL_TEXT        # every value distinct -> text
    assert p.col("grade").kind == COL_CATEGORY  # repeats -> category


def test_small_dataset_category_not_misread_as_text():
    # 5 distinct in 7 rows: ratio is high but it's clearly a category
    rows = [{"age": a} for a in
            ["25-34", "35-44", "18-24", "45-54", "25-34", "35-44", "55+"]]
    p = profile_data(rows)
    assert p.col("age").kind == COL_CATEGORY


def test_empty_and_blank_handling():
    rows = [{"x": "", "y": "5"}, {"x": None, "y": "10"}, {"x": "", "y": "15"}]
    p = profile_data(rows)
    assert p.col("x").kind == COL_EMPTY
    assert p.col("y").kind == COL_NUMBER
    assert p.col("y").total == 30.0


def test_does_not_crash_on_empty():
    p = profile_data([])
    assert p.n_rows == 0 and p.columns == []


def test_ragged_rows_union_of_columns():
    rows = [{"a": "1"}, {"a": "2", "b": "x"}, {"b": "y"}]
    p = profile_data(rows)
    names = {c.name for c in p.columns}
    assert names == {"a", "b"}


def test_date_preferred_over_number_for_year_like():
    # full dates should be dates, not numbers
    rows = [{"d": "2026-01-05"}, {"d": "2026-02-05"}, {"d": "2026-03-05"}]
    p = profile_data(rows)
    assert p.col("d").kind == COL_DATE


def test_mostly_numeric_with_noise_still_number():
    # 80% threshold: 4 of 5 numeric -> number
    rows = [{"v": "10"}, {"v": "20"}, {"v": "30"}, {"v": "40"}, {"v": "N/A"}]
    p = profile_data(rows)
    assert p.col("v").kind == COL_NUMBER
    assert p.col("v").count == 5 and p.col("v").total == 100.0


# ---- generic report rendering (layer 2) ---- #
from engine.generic_report import build_generic_html, summarize


def _rows(name):
    from engine.profile import load_csv_rows
    return load_csv_rows(f"/tmp/testdata/{name}.csv")


def test_generic_report_renders_for_numeric_data():
    rows = _rows("sales")
    prof = profile_data(rows)
    html = build_generic_html(rows, prof, brand="Acme", title="Sales")
    assert "<h1>Acme</h1>" in html
    assert "Trend over time" in html          # has a date col -> trend
    assert "Breakdown by" in html
    assert "26,000" in html                    # revenue total in a KPI


def test_generic_report_handles_no_numeric_data():
    rows = _rows("survey")
    prof = profile_data(rows)
    html = build_generic_html(rows, prof, brand="Survey", title="Results")
    assert "Trend over time" not in html       # no date/numeric -> no trend
    assert "Breakdown by" in html              # falls back to counts
    assert "71.4%" in html                      # Yes share by count


def test_generic_report_never_crashes_on_minimal_data():
    rows = [{"x": "1"}, {"x": "2"}]
    prof = profile_data(rows)
    html = build_generic_html(rows, prof)
    assert "<h1>" in html and "Records" in html
