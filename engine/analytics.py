"""
analytics.py — Facebook/Meta Ads performance analytics engine.

Parses a Meta Ads Manager CSV export and computes the standard marketing
metrics advertisers report on. Hardened to survive real-world exports:
flexible column matching, value normalization, validation, and clear errors
instead of silent wrong numbers or ugly crashes.

Author: Jared Jowett
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from collections import defaultdict

log = logging.getLogger("analytics")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ReportError(Exception):
    """Raised when the input can't be turned into a valid report."""


# --------------------------------------------------------------------------- #
# Column mapping — real Meta exports vary by account, language, and settings.
# Each logical field maps to a list of header variants we'll accept (matched
# case-insensitively, ignoring surrounding whitespace and currency suffixes).
# --------------------------------------------------------------------------- #
COLUMN_ALIASES = {
    "day":         ["day", "date", "reporting starts", "reporting_starts"],
    "campaign":    ["campaign name", "campaign", "campaign_name", "ad set name", "ad name"],
    "objective":   ["objective", "campaign objective", "optimization goal"],
    "impressions": ["impressions", "impr."],
    "reach":       ["reach", "people reached"],
    "clicks":      ["link clicks", "clicks (all)", "clicks", "link_clicks"],
    "spend":       ["amount spent", "amount spent (usd)", "spend", "cost"],
    "conversions": ["results", "conversions", "purchases", "website purchases"],
    "conv_value":  ["conversion value", "conversion value (usd)", "purchases conversion value",
                    "website purchases conversion value", "value"],
}

REQUIRED = ["campaign", "impressions", "spend"]   # minimum to produce a report


def _norm_header(h: str) -> str:
    """Lowercase, strip, and drop trailing currency tags like '(usd)'."""
    h = (h or "").strip().lower()
    # strip a trailing parenthetical currency code: "amount spent (usd)" -> "amount spent"
    if h.endswith(")") and "(" in h:
        inside = h[h.rfind("(") + 1:-1].strip()
        if len(inside) <= 4 and inside.isalpha():   # looks like a currency code
            h = h[:h.rfind("(")].strip()
    return h


def build_column_map(headers: list[str]) -> dict[str, str]:
    """
    Map our logical field names to the actual headers present in this CSV.
    Returns {logical_name: actual_header}. Raises if a required field is missing.
    """
    norm_to_actual = {_norm_header(h): h for h in headers}
    mapping: dict[str, str] = {}

    for logical, variants in COLUMN_ALIASES.items():
        for variant in variants:
            if variant in norm_to_actual:
                mapping[logical] = norm_to_actual[variant]
                break

    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise ReportError(
            f"CSV is missing required column(s): {', '.join(missing)}. "
            f"Found headers: {', '.join(headers)}"
        )

    found = ", ".join(f"{k}->'{v}'" for k, v in mapping.items())
    log.info("column map resolved: %s", found)
    return mapping


# --------------------------------------------------------------------------- #
# Value normalization
# --------------------------------------------------------------------------- #
def _to_float(raw, field: str, line: int) -> float:
    """
    Parse a numeric value from messy export data:
    handles '$', ',', '1.2K', '(USD)', blanks, and stray text.
    Negative values are clamped to 0 and warned (ad metrics can't be negative).
    """
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if s in ("", "-", "—", "N/A", "n/a", "null", "None"):
        return 0.0

    mult = 1.0
    s2 = s.replace("$", "").replace(",", "").replace("%", "").strip()
    if s2 and s2[-1] in "KkMm":
        mult = {"k": 1e3, "m": 1e6}[s2[-1].lower()]
        s2 = s2[:-1]

    try:
        val = float(s2) * mult
    except ValueError:
        log.warning("line %d: couldn't parse %s value %r — treating as 0", line, field, raw)
        return 0.0

    if val < 0:
        log.warning("line %d: negative %s (%s) — clamped to 0", line, field, val)
        return 0.0
    return val


def _to_int(raw, field: str, line: int) -> int:
    return int(round(_to_float(raw, field, line)))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
@dataclass
class Metrics:
    spend: float = 0.0
    impressions: int = 0
    reach: int = 0
    clicks: int = 0
    conversions: int = 0
    conv_value: float = 0.0

    def add(self, *, spend, impressions, reach, clicks, conversions, conv_value):
        self.spend += spend
        self.impressions += impressions
        self.reach += reach
        self.clicks += clicks
        self.conversions += conversions
        self.conv_value += conv_value

    @property
    def ctr(self):  return (self.clicks / self.impressions * 100) if self.impressions else 0.0
    @property
    def cpc(self):  return (self.spend / self.clicks) if self.clicks else 0.0
    @property
    def cpm(self):  return (self.spend / self.impressions * 1000) if self.impressions else 0.0
    @property
    def cpa(self):  return (self.spend / self.conversions) if self.conversions else 0.0
    @property
    def cvr(self):  return (self.conversions / self.clicks * 100) if self.clicks else 0.0
    @property
    def roas(self): return (self.conv_value / self.spend) if self.spend else 0.0
    @property
    def frequency(self): return (self.impressions / self.reach) if self.reach else 0.0


@dataclass
class Report:
    overall: Metrics
    by_campaign: dict
    by_day: dict
    objectives: dict
    date_range: tuple
    warnings: list           # collected data-quality notes
    rows_read: int
    rows_skipped: int

    @property
    def best_roas_campaign(self):
        c = {k: v for k, v in self.by_campaign.items() if v.spend > 0}
        return max(c.items(), key=lambda kv: kv[1].roas) if c else None

    @property
    def worst_roas_campaign(self):
        c = {k: v for k, v in self.by_campaign.items() if v.spend > 0}
        return min(c.items(), key=lambda kv: kv[1].roas) if c else None


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_report(csv_path: str) -> Report:
    """
    Load and validate a Meta Ads CSV export into a Report.
    Raises ReportError for unusable input; collects soft issues as warnings.
    """
    try:
        f = open(csv_path, newline="", encoding="utf-8-sig")
    except FileNotFoundError:
        raise ReportError(f"File not found: {csv_path}")
    except OSError as e:
        raise ReportError(f"Could not open {csv_path}: {e}")

    warnings: list[str] = []
    with f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ReportError("CSV appears to be empty (no header row).")

        colmap = build_column_map(reader.fieldnames)

        overall = Metrics()
        by_campaign: dict[str, Metrics] = defaultdict(Metrics)
        by_day: dict[str, Metrics] = defaultdict(Metrics)
        objectives: dict[str, str] = {}
        days: set[str] = set()
        rows_read = rows_skipped = 0

        def get(row, logical):
            col = colmap.get(logical)
            return row.get(col) if col else None

        for i, row in enumerate(reader, start=2):   # line 1 is the header
            campaign = (get(row, "campaign") or "").strip()
            if not campaign:
                rows_skipped += 1
                continue

            vals = dict(
                spend       = _to_float(get(row, "spend"),       "spend", i),
                impressions = _to_int(get(row, "impressions"),   "impressions", i),
                reach       = _to_int(get(row, "reach"),         "reach", i),
                clicks      = _to_int(get(row, "clicks"),        "clicks", i),
                conversions = _to_int(get(row, "conversions"),   "conversions", i),
                conv_value  = _to_float(get(row, "conv_value"),  "conv_value", i),
            )
            # sanity: clicks shouldn't exceed impressions
            if vals["clicks"] > vals["impressions"] and vals["impressions"] > 0:
                warnings.append(f"line {i}: clicks ({vals['clicks']}) exceed impressions "
                                f"({vals['impressions']}) for '{campaign}'")

            overall.add(**vals)
            by_campaign[campaign].add(**vals)
            objectives[campaign] = (get(row, "objective") or "").strip()

            day = (get(row, "day") or "").strip()
            if day:
                by_day[day].add(**vals)
                days.add(day)
            rows_read += 1

    if rows_read == 0:
        raise ReportError("No usable data rows found in the CSV.")

    if rows_skipped:
        warnings.append(f"{rows_skipped} row(s) skipped (blank campaign name).")
    if not days:
        warnings.append("No date column found — daily trend will be unavailable.")

    date_range = (min(days), max(days)) if days else ("", "")
    log.info("loaded %d rows (%d skipped) across %d campaigns",
             rows_read, rows_skipped, len(by_campaign))
    for w in warnings:
        log.warning(w)

    return Report(
        overall=overall,
        by_campaign=dict(by_campaign),
        by_day=dict(sorted(by_day.items())),
        objectives=objectives,
        date_range=date_range,
        warnings=warnings,
        rows_read=rows_read,
        rows_skipped=rows_skipped,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    r = load_report("sample_fb_export.csv")
    o = r.overall
    print(f"\nSpend ${o.spend:,.2f} | ROAS {o.roas:.2f}x | CTR {o.ctr:.2f}% "
          f"| CPA ${o.cpa:.2f} | {r.rows_read} rows, {len(r.by_campaign)} campaigns")
    if r.warnings:
        print("warnings:", *r.warnings, sep="\n  ")
