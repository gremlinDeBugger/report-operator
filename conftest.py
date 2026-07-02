import sys, os, csv
sys.path.insert(0, os.path.dirname(__file__))

import pytest

# ---------------------------------------------------------------------------
# Test fixture data written to /tmp/testdata/ at session start.
#
# The tests reference /tmp/testdata/{sales,survey,sensor}.csv directly as
# hardcoded paths. This fixture creates those files so the suite is
# self-contained and works in any clean environment (CI, containers, etc.)
# without requiring pre-existing files on disk.
# ---------------------------------------------------------------------------

_TESTDATA_DIR = "/tmp/testdata"

_FIXTURES = {
    # Sales data: Date + Category + two Numeric columns.
    # Revenue sums to exactly 26,000 (test_generic_report_renders_for_numeric_data
    # asserts '26,000' appears in the KPI block). No Meta ad column names so
    # looks_like_meta() correctly returns False.
    "sales.csv": {
        "headers": ["Date", "Region", "Product", "Units", "Revenue"],
        "rows": [
            ["2026-01-05", "NE", "Widget A", "120", "3600"],
            ["2026-01-12", "NE", "Widget B", "140", "4200"],
            ["2026-01-19", "SW", "Widget A", "100", "3000"],
            ["2026-01-26", "MW", "Widget B", "75",  "2250"],
            ["2026-02-02", "NE", "Widget A", "110", "3300"],
            ["2026-02-09", "SW", "Widget B", "85",  "2550"],
            ["2026-02-16", "MW", "Widget A", "60",  "1800"],
            ["2026-02-23", "NE", "Widget B", "90",  "2700"],
            ["2026-03-02", "SW", "Widget A", "80",  "2600"],
        ],
    },
    # Survey data: all categorical, no dates or numbers.
    # 'Yes' appears 5 of 7 times → 71.4% share in the breakdown
    # (test_generic_report_handles_no_numeric_data asserts '71.4%' in html).
    "survey.csv": {
        "headers": ["Respondent", "Satisfied", "Department"],
        "rows": [
            ["R001", "Yes", "Marketing"],
            ["R002", "Yes", "Sales"],
            ["R003", "Yes", "Marketing"],
            ["R004", "No",  "Engineering"],
            ["R005", "Yes", "Sales"],
            ["R006", "Yes", "Marketing"],
            ["R007", "No",  "Engineering"],
        ],
    },
    # Sensor data: generic non-meta CSV used only to verify HTML is produced.
    "sensor.csv": {
        "headers": ["Timestamp", "Sensor", "Value", "Unit"],
        "rows": [
            ["2026-06-01 08:00:00", "Temp_A", "22.5", "C"],
            ["2026-06-01 08:05:00", "Temp_A", "23.1", "C"],
            ["2026-06-01 08:10:00", "Temp_B", "21.8", "C"],
            ["2026-06-01 08:15:00", "Temp_B", "22.0", "C"],
            ["2026-06-01 08:20:00", "Temp_A", "23.5", "C"],
        ],
    },
}


@pytest.fixture(scope="session", autouse=True)
def create_testdata():
    """Create /tmp/testdata/ fixture CSVs before any test runs."""
    os.makedirs(_TESTDATA_DIR, exist_ok=True)
    for filename, spec in _FIXTURES.items():
        path = os.path.join(_TESTDATA_DIR, filename)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(spec["headers"])
            w.writerows(spec["rows"])
