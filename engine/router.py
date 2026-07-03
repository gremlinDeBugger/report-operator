"""
router.py — Decides which report a CSV becomes, and renders it.

The customer DECLARES what their data is at intake (report_type). That declaration
is authoritative — the system honors it rather than guessing. Column-sniffing is
only a fallback for when the type is "auto"/unset, or when a declared specialized
type doesn't actually fit the data (in which case we fall back to generic rather
than producing a broken report).

Destinations live now:
    "meta"     -> the sharp Meta Ads template (engine.report)
    "generic"  -> the profiler-driven generic report (engine.generic_report)
    "auto"     -> sniff: looks like Meta? -> meta, else -> generic

Declared-type slots ready to add (each a contained template, build when needed):
    "sales", "catalog", "survey", ...
Until a specialized template exists, a declared type that isn't "meta" routes to
the generic engine — so declaring "sales" today gives a clean generic report, and
upgrades to a sales-specific one the day that template is built. No rebuild.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("router")

# types that currently have their own specialized template
SPECIALIZED = {"meta", "financial"}
# declared types we accept (specialized ones render sharp; the rest -> generic for now)
KNOWN_TYPES = {"auto", "generic", "meta", "financial", "sales", "catalog", "survey"}


@dataclass
class RoutedReport:
    report_type: str          # what it was declared as
    rendered_as: str          # what it actually rendered as (meta/generic)
    html_path: str
    pdf_path: str | None = None
    note: str = ""            # human-readable reason, surfaced for transparency


def looks_like_meta(headers: list[str]) -> bool:
    """Conservative sniff: only True when the CSV clearly carries Meta ad data
    (the engine's own required fields all resolve)."""
    try:
        from engine.analytics import build_column_map
        build_column_map(headers)   # raises if required Meta fields are missing
        return True
    except Exception:
        return False


def looks_like_financial(headers: list[str]) -> bool:
    """True when the CSV carries the connector's company-quarter contract."""
    try:
        from engine.financial_report import looks_like_fundamentals
        return looks_like_fundamentals(headers)
    except Exception:
        return False


def decide(report_type: str, headers: list[str]) -> tuple[str, str]:
    """
    Return (rendered_as, note). rendered_as is 'meta' or 'generic' (the only two
    live renderers). note explains the decision for transparency.
    """
    rt = (report_type or "auto").strip().lower()
    if rt not in KNOWN_TYPES:
        rt = "auto"

    if rt == "meta":
        if looks_like_meta(headers):
            return "meta", "declared 'meta'; ad columns confirmed"
        return "generic", ("declared 'meta' but required ad columns "
                           "(campaign/impressions/spend) weren't found — "
                           "rendered as generic instead")
    if rt == "financial":
        if looks_like_financial(headers):
            return "financial", "declared 'financial'; fundamentals columns confirmed"
        return "generic", ("declared 'financial' but fundamentals columns "
                           "(ticker/fiscal_date/revenue) weren't found — "
                           "rendered as generic instead")
    if rt == "generic":
        return "generic", "declared 'generic'"
    if rt == "auto":
        if looks_like_meta(headers):
            return "meta", "auto-detected Meta ad data"
        if looks_like_financial(headers):
            return "financial", "auto-detected quarterly fundamentals data"
        return "generic", "auto: no specialized type matched — generic report"

    # a declared specialized type that has no template yet (sales/catalog/survey)
    if rt in SPECIALIZED:                 # future-proofing if SPECIALIZED grows
        return rt, f"declared '{rt}'"
    return "generic", (f"declared '{rt}' — no specialized template yet, "
                       f"rendered as a generic report")


def render(csv_path: str, out_dir: str, *, report_type: str = "auto",
           brand: str = "Your Report", branding=None, insight_text: str = "",
           file_stem: str = "report", make_pdf: bool = True) -> RoutedReport:
    """
    Route the CSV to the right renderer and produce HTML (+PDF). Returns a
    RoutedReport describing what happened, including the routing note.
    """
    import os
    os.makedirs(out_dir, exist_ok=True)

    # read headers once to decide
    from engine.profile import load_csv_rows
    rows = load_csv_rows(csv_path)
    headers = list(rows[0].keys()) if rows else []
    rendered_as, note = decide(report_type, headers)
    log.info("routing %s -> %s (%s)", os.path.basename(csv_path), rendered_as, note)

    html_path = os.path.join(out_dir, f"{file_stem}.html")

    if rendered_as == "meta":
        from engine.analytics import load_report
        from engine.report import write_html, Branding
        report = load_report(csv_path)
        b = branding or Branding(brand=brand)
        write_html(report, html_path, branding=b, insight_text=insight_text)
    elif rendered_as == "financial":
        from engine.financial_report import write_financial_html
        from engine.report import Branding
        b = branding or Branding(brand=brand)
        write_financial_html(rows, html_path, brand=brand, branding=b,
                             insight_text=insight_text)
    else:
        from engine.profile import profile_data
        from engine.generic_report import write_generic_html
        from engine.report import Branding
        prof = profile_data(rows)
        b = branding or Branding(brand=brand)
        # title reflects the declared type when meaningful
        title = {"sales": "Sales Report", "catalog": "Catalog Report",
                 "survey": "Survey Results"}.get(
                     (report_type or "").lower(), "Data Report")
        write_generic_html(rows, prof, html_path, brand=brand, branding=b,
                           insight_text=insight_text, title=title)

    pdf_path = None
    if make_pdf:
        try:
            from engine.make_pdf import html_to_pdf
            pdf_path = os.path.join(out_dir, f"{file_stem}.pdf")
            html_to_pdf(os.path.abspath(html_path), os.path.abspath(pdf_path))
        except Exception as e:
            log.warning("PDF skipped (%s)", e)
            pdf_path = None

    return RoutedReport(report_type=report_type, rendered_as=rendered_as,
                        html_path=html_path, pdf_path=pdf_path, note=note)
