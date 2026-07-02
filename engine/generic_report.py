"""
generic_report.py — Renders ANY profiled dataset into a branded report.

Driven entirely by the profiler's understanding of the data (profile.py), this
produces the same visual language as the Meta report — dark header, KPI grid,
charts, breakdown table — but for arbitrary data it has no domain knowledge of.

How it decides what to show:
  - KPIs        : the dataset's headline numbers — row count plus the totals of
                  the most significant numeric columns.
  - Trend chart : if there's a date column, the main numeric column summed over
                  time (a real time-series). Omitted if no date column.
  - Breakdowns  : for the top category columns, the leading numeric broken down
                  by category (e.g. revenue by region), or simple value counts
                  when there's no numeric to sum.
  - Table       : the category breakdown as numbers.

Branding (logo/contact) and the AI insight block are shared with the Meta report,
so a generic report looks like a sibling, not a different product.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import html as _html
from collections import defaultdict

try:
    from engine.profile import (profile_data, DataProfile, try_number,
                                COL_NUMBER, COL_CATEGORY, COL_DATE)
    from engine.report import Branding
except ImportError:
    from profile import (profile_data, DataProfile, try_number,
                         COL_NUMBER, COL_CATEGORY, COL_DATE)
    from report import Branding


def _fmt_num(n: float) -> str:
    if n is None:
        return "—"
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n:,.0f}"
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}"


def _spark_points(values: list[float], w: int = 760, h: int = 150, pad: int = 8) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    n = len(values)
    step = (w - 2 * pad) / max(n - 1, 1)
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = h - pad - (v - lo) / span * (h - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


# --------------------------------------------------------------------------- #
# Aggregation helpers (work off raw rows + the profile)
# --------------------------------------------------------------------------- #
def _sum_by_category(rows, cat_col, num_col):
    agg = defaultdict(float)
    for r in rows:
        key = str(r.get(cat_col, "")).strip() or "(blank)"
        val = try_number(r.get(num_col))
        if val is not None:
            agg[key] += val
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)


def _count_by_category(rows, cat_col):
    agg = defaultdict(int)
    for r in rows:
        key = str(r.get(cat_col, "")).strip() or "(blank)"
        agg[key] += 1
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)


def _sum_over_time(rows, date_col, num_col):
    agg = defaultdict(float)
    for r in rows:
        d = str(r.get(date_col, "")).strip()
        if not d:
            continue
        val = try_number(r.get(num_col)) if num_col else 1
        agg[d] += (val if val is not None else 0)
    return sorted(agg.items())   # chronological-ish by string; dates are ISO-sortable


def summarize(rows: list[dict], profile: DataProfile) -> dict:
    """Build the structured content the template renders. Pure data, no HTML."""
    # pick the 'primary' numeric = the one with the largest total magnitude
    primary_num = None
    if profile.numeric_columns:
        primary_num = max(
            profile.numeric_columns,
            key=lambda name: abs(profile.col(name).total or 0),
        )
    # top categories by how informative they are (fewest blanks, sensible distinct)
    cats = sorted(profile.category_columns,
                  key=lambda name: profile.col(name).distinct)
    return {
        "primary_num": primary_num,
        "categories": cats[:3],
        "date_col": profile.date_column,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
class InsufficientData(Exception):
    """Raised when a dataset is too thin to produce a meaningful report."""


def is_reportable(profile) -> tuple[bool, str]:
    """
    A dataset is reportable if there's actually something to report: at least a
    few rows AND at least one numeric, date, or usable category column. A handful
    of one-off text columns (or a single row) produces only a hollow report, so
    we reject it with a clear reason rather than emit something that looks broken.
    """
    if profile.n_rows < 2:
        return False, "needs at least 2 rows of data"
    has_signal = bool(profile.numeric_columns or profile.category_columns
                      or profile.date_column)
    if not has_signal:
        return False, ("no numeric, date, or category columns found — nothing "
                       "to summarize (looks like free text or unstructured data)")
    return True, ""


def build_generic_html(rows: list[dict], profile: DataProfile,
                       brand: str = "Your Report",
                       branding: "Branding | None" = None,
                       insight_text: str = "",
                       title: str = "Data Report") -> str:
    ok, why = is_reportable(profile)
    if not ok:
        raise InsufficientData(why)
    if branding is None:
        branding = Branding(brand=brand)
    plan = summarize(rows, profile)

    # ---- shared branding fragments (mirror report.py) ---- #
    _logo = branding.logo_data_uri()
    logo_html = f'<img class="brand-logo" src="{_logo}" alt="">' if _logo else ""
    _cb = []
    if branding.business_name:
        _cb.append(f'<span class="biz">{_html.escape(branding.business_name)}</span>')
    if branding.email:
        _cb.append(_html.escape(branding.email))
    if branding.phone:
        _cb.append(_html.escape(branding.phone))
    contact_html = f'<div class="contact">{" · ".join(_cb)}</div>' if _cb else ""
    insight_html = (f'<div class="insight"><div class="insight-tag">Analysis</div>'
                    f'<p>{_html.escape(insight_text)}</p></div>') if insight_text else ""

    e_brand = _html.escape(branding.brand)
    e_title = _html.escape(title)

    # ---- KPIs: row count + top numeric totals ---- #
    kpi_cells = [_kpi("Records", _fmt_num(profile.n_rows), f"{profile.n_cols} fields")]
    for name in (profile.numeric_columns or [])[:2]:
        c = profile.col(name)
        kpi_cells.append(_kpi(name, _fmt_num(c.total),
                              f"avg {_fmt_num(c.mean)} · max {_fmt_num(c.maximum)}"))
    # pad to 3
    while len(kpi_cells) < 3 and plan["categories"]:
        cat = plan["categories"][len(kpi_cells) - 1] if len(kpi_cells) - 1 < len(plan["categories"]) else None
        if not cat:
            break
        cc = profile.col(cat)
        kpi_cells.append(_kpi(cat, _fmt_num(cc.distinct), "distinct values"))
    while len(kpi_cells) < 3:
        kpi_cells.append('<div class="kpi"></div>')
    kpis = "".join(kpi_cells[:3])

    # ---- trend chart (only if a date column exists) ---- #
    trend_html = ""
    if plan["date_col"]:
        series = _sum_over_time(rows, plan["date_col"], plan["primary_num"])
        if len(series) >= 2:
            vals = [v for _, v in series]
            label = plan["primary_num"] or "Records"
            trend_html = f"""
    <div class="section-title">Trend over time</div>
    <div class="chart-card">
      <div class="chart-head"><h3>{_html.escape(label)} over {_html.escape(plan['date_col'])}</h3>
        <div class="legend"><b>● {_html.escape(label)}</b></div></div>
      <svg viewBox="0 0 760 150" preserveAspectRatio="none">
        <polyline fill="none" stroke="#4f7cff" stroke-width="2.5" points="{_spark_points(vals)}"/>
        <text class="axis" x="6" y="12">peak {_fmt_num(max(vals))}</text>
        <text class="axis" x="6" y="146">{_html.escape(series[0][0])}</text>
        <text class="axis" x="660" y="146">{_html.escape(series[-1][0])}</text>
      </svg>
    </div>"""

    # ---- breakdown table for the leading category ---- #
    breakdown_html = ""
    if plan["categories"]:
        cat = plan["categories"][0]
        num = plan["primary_num"]
        if num:
            data = _sum_by_category(rows, cat, num)[:12]
            head = f"<th>{_html.escape(cat)}</th><th>{_html.escape(num)}</th><th>share</th>"
            total = sum(v for _, v in data) or 1
            body = "".join(
                f'<tr><td class="c-name"><span class="dot"></span>{_html.escape(str(k))}</td>'
                f'<td class="num">{_fmt_num(v)}</td>'
                f'<td class="num">{v/total*100:.1f}%</td></tr>'
                for k, v in data)
        else:
            data = _count_by_category(rows, cat)[:12]
            head = f"<th>{_html.escape(cat)}</th><th>count</th><th>share</th>"
            total = sum(v for _, v in data) or 1
            body = "".join(
                f'<tr><td class="c-name"><span class="dot"></span>{_html.escape(str(k))}</td>'
                f'<td class="num">{v:,}</td>'
                f'<td class="num">{v/total*100:.1f}%</td></tr>'
                for k, v in data)
        breakdown_html = f"""
    <div class="section-title">Breakdown by {_html.escape(cat)}</div>
    <table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"""

    from engine.report import build_html as _meta_build  # reuse the <style> block
    style = _extract_style()

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e_brand} — {e_title}</title>
{style}
</head><body><div class="wrap">
  <header>
    <div class="header-row">
      <div class="header-main">
        <div class="eyebrow">{e_title}</div>
        <h1>{e_brand}</h1>
        <div class="range">{profile.n_rows:,} records · {profile.n_cols} fields</div>
      </div>
      {logo_html}
    </div>
    {contact_html}
  </header>
  <div class="kpis">{kpis}</div>
  <div class="body">
    {insight_html}
    {trend_html}
    {breakdown_html}
  </div>
  <footer>
    <span>Prepared for {e_brand}</span>
    <span>Generated by GremlinHunter Reporting</span>
  </footer>
</div></body></html>"""


def _kpi(label, value, sub=""):
    return (f'<div class="kpi"><div class="kpi-label">{_html.escape(str(label))}</div>'
            f'<div class="kpi-value">{_html.escape(str(value))}</div>'
            f'<div class="kpi-sub">{_html.escape(str(sub))}</div></div>')


_STYLE_CACHE = None
def _extract_style() -> str:
    """Pull the <style>...</style> block out of the Meta template so the generic
    report shares the exact same look without duplicating CSS."""
    global _STYLE_CACHE
    if _STYLE_CACHE:
        return _STYLE_CACHE
    import os
    here = os.path.dirname(__file__)
    src = open(os.path.join(here, "report.py"), encoding="utf-8").read()
    start = src.find("<style>")
    end = src.find("</style>", start)
    raw = src[start:end + len("</style>")]
    # the Meta template doubles braces for .format(); undo that for direct use
    _STYLE_CACHE = raw.replace("{{", "{").replace("}}", "}")
    return _STYLE_CACHE


def write_generic_html(rows, profile, path, brand="Your Report",
                       branding=None, insight_text="", title="Data Report"):
    html = build_generic_html(rows, profile, brand, branding, insight_text, title)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
