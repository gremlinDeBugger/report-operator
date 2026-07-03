"""
financial_report.py — Fixed-format quarterly fundamentals report.

The first specialized template to land in the router's ready-to-add slots
(report_type="financial"). Reads the connector's normalized company-quarter CSV
and renders the SAME fixed structure for every company, every quarter, every
run — which is the point: a client comparing Apple's report to Microsoft's is
comparing numbers, never layouts.

Per company, the format is:
    KPI row      : latest-quarter Revenue, Net income, Net margin, EPS
    Deltas       : QoQ and YoY revenue growth, YoY EPS growth
    Trend        : revenue over the quarters on file (sparkline)
    Quarter table: every quarter's revenue / net income / margin / EPS

financial_metrics_payload() is the single source of numbers for BOTH the AI
insight prompt and the verification gate — generation and verification read the
same figures by construction.

Shares the visual language (and literally the CSS) of the Meta and generic
templates: a financial report looks like a sibling, not a different product.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import html as _html
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from engine.report import Branding
    from engine.generic_report import _extract_style, _kpi, _spark_points
    from engine.profile import try_number
except ImportError:                                    # direct-run convenience
    from report import Branding
    from generic_report import _extract_style, _kpi, _spark_points
    from profile import try_number

REQUIRED = {"ticker", "fiscal_date", "revenue"}
NUMERIC = ["revenue", "gross_profit", "operating_income", "net_income", "eps",
           "gross_margin_pct", "operating_margin_pct", "net_margin_pct"]


def looks_like_fundamentals(headers: list[str]) -> bool:
    hs = {h.strip().lower() for h in (headers or [])}
    return REQUIRED.issubset(hs)


# --------------------------------------------------------------------------- #
# Load + compute
# --------------------------------------------------------------------------- #
@dataclass
class Quarter:
    fiscal_date: str
    period: str
    revenue: float | None
    net_income: float | None
    eps: float | None
    net_margin_pct: float | None
    gross_margin_pct: float | None = None
    operating_income: float | None = None


@dataclass
class Company:
    ticker: str
    quarters: list[Quarter] = field(default_factory=list)   # oldest -> newest

    @property
    def latest(self) -> Quarter:
        return self.quarters[-1]

    def _growth(self, attr: str, back: int) -> float | None:
        """Percent change of `attr` vs `back` quarters ago; None if undefined."""
        if len(self.quarters) <= back:
            return None
        now = getattr(self.quarters[-1], attr)
        then = getattr(self.quarters[-1 - back], attr)
        if now is None or then in (None, 0):
            return None
        return round((now - then) / abs(then) * 100.0, 1)

    @property
    def revenue_qoq_pct(self): return self._growth("revenue", 1)
    @property
    def revenue_yoy_pct(self): return self._growth("revenue", 4)
    @property
    def eps_yoy_pct(self): return self._growth("eps", 4)


class FundamentalsError(Exception):
    pass


def load_fundamentals(rows: list[dict]) -> list[Company]:
    """Normalized connector rows -> per-company series, oldest->newest.
    Raises FundamentalsError if the data can't support the fixed format."""
    if not rows:
        raise FundamentalsError("no rows")
    if not looks_like_fundamentals(list(rows[0].keys())):
        raise FundamentalsError(
            "not fundamentals data — need at least ticker/fiscal_date/revenue")
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        t = (r.get("ticker") or "").strip().upper()
        if t and (r.get("fiscal_date") or "").strip():
            by[t].append(r)
    companies = []
    for t, rs in sorted(by.items()):
        rs.sort(key=lambda r: r["fiscal_date"])
        qs = [Quarter(fiscal_date=r["fiscal_date"], period=r.get("period", ""),
                      revenue=try_number(r.get("revenue")),
                      net_income=try_number(r.get("net_income")),
                      eps=try_number(r.get("eps")),
                      net_margin_pct=try_number(r.get("net_margin_pct")),
                      gross_margin_pct=try_number(r.get("gross_margin_pct")),
                      operating_income=try_number(r.get("operating_income")))
              for r in rs]
        qs = [q for q in qs if q.revenue is not None]
        if len(qs) >= 2:                       # a "trend" needs two points
            companies.append(Company(ticker=t, quarters=qs))
    if not companies:
        raise FundamentalsError(
            "no company had 2+ quarters with revenue — too thin for the "
            "fixed format")
    return companies


# --------------------------------------------------------------------------- #
# The single source of numbers — insight prompt AND verification read this
# --------------------------------------------------------------------------- #
def financial_metrics_payload(companies: list[Company],
                              market_context: str | dict | None = None) -> dict:
    out = {"companies": []}
    for c in companies:
        L = c.latest
        out["companies"].append({
            "ticker": c.ticker,
            "quarters_on_file": len(c.quarters),
            "latest": {"fiscal_date": L.fiscal_date, "period": L.period,
                       "revenue": L.revenue, "net_income": L.net_income,
                       "eps": L.eps, "net_margin_pct": L.net_margin_pct,
                       "gross_margin_pct": L.gross_margin_pct},
            "revenue_qoq_pct": c.revenue_qoq_pct,
            "revenue_yoy_pct": c.revenue_yoy_pct,
            "eps_yoy_pct": c.eps_yoy_pct,
            "revenue_series": [q.revenue for q in c.quarters],
        })
    if market_context:
        out["market_context"] = market_context
    return out


# --------------------------------------------------------------------------- #
# Render — one fixed structure, repeated per company
# --------------------------------------------------------------------------- #
def _money(n) -> str:
    if n is None:
        return "—"
    a = abs(n)
    if a >= 1_000_000_000:
        return f"${n/1_000_000_000:,.2f}B"
    if a >= 1_000_000:
        return f"${n/1_000_000:,.1f}M"
    return f"${n:,.0f}"


def _pctf(n) -> str:
    return "—" if n is None else f"{n:+.1f}%"


def _delta_class(n) -> str:
    if n is None:
        return ""
    return "up" if n >= 0 else "down"


def _company_section(c: Company) -> str:
    L = c.latest
    kpis = "".join([
        _kpi("Revenue", _money(L.revenue), f"{L.period} {L.fiscal_date}"),
        _kpi("Net income", _money(L.net_income),
             f"margin {L.net_margin_pct:.1f}%" if L.net_margin_pct is not None else ""),
        _kpi("EPS", f"{L.eps:.2f}" if L.eps is not None else "—",
             f"YoY {_pctf(c.eps_yoy_pct)}" if c.eps_yoy_pct is not None else ""),
    ])

    callouts = f"""
        <div class="callout {_delta_class(c.revenue_yoy_pct)}">
          <div class="co-tag">Revenue YoY</div>
          <div class="co-name">{_pctf(c.revenue_yoy_pct)}</div>
          <div class="co-metric">vs same quarter last year</div>
        </div>
        <div class="callout {_delta_class(c.revenue_qoq_pct)}">
          <div class="co-tag">Revenue QoQ</div>
          <div class="co-name">{_pctf(c.revenue_qoq_pct)}</div>
          <div class="co-metric">vs prior quarter</div>
        </div>"""

    vals = [q.revenue for q in c.quarters]
    trend = f"""
    <div class="chart-card">
      <div class="chart-head"><h3>Revenue by quarter</h3>
        <div class="legend"><b>● revenue</b></div></div>
      <svg viewBox="0 0 760 150" preserveAspectRatio="none">
        <polyline fill="none" stroke="#4f7cff" stroke-width="2.5"
                  points="{_spark_points(vals)}"/>
        <text class="axis" x="6" y="12">peak {_money(max(vals))}</text>
        <text class="axis" x="6" y="146">{_html.escape(c.quarters[0].fiscal_date)}</text>
        <text class="axis" x="640" y="146">{_html.escape(c.quarters[-1].fiscal_date)}</text>
      </svg>
    </div>"""

    body = "".join(
        f'<tr><td class="c-name"><span class="dot"></span>'
        f'{_html.escape(q.period)} {_html.escape(q.fiscal_date)}</td>'
        f'<td class="num">{_money(q.revenue)}</td>'
        f'<td class="num">{_money(q.net_income)}</td>'
        f'<td class="num">{q.net_margin_pct:.1f}%</td>'
        f'<td class="num">{q.eps:.2f}</td></tr>'
        if q.net_margin_pct is not None and q.eps is not None else
        f'<tr><td class="c-name"><span class="dot"></span>'
        f'{_html.escape(q.period)} {_html.escape(q.fiscal_date)}</td>'
        f'<td class="num">{_money(q.revenue)}</td>'
        f'<td class="num">{_money(q.net_income)}</td>'
        f'<td class="num">—</td><td class="num">—</td></tr>'
        for q in reversed(c.quarters))
    table = f"""
    <table><thead><tr><th>Quarter</th><th>Revenue</th><th>Net income</th>
    <th>Net margin</th><th>EPS</th></tr></thead><tbody>{body}</tbody></table>"""

    return f"""
    <div class="section-title">{_html.escape(c.ticker)} — Quarterly fundamentals</div>
    <div class="kpis" style="margin-bottom:18px">{kpis}</div>
    <div style="display:flex; gap:14px; margin-bottom:20px">{callouts}</div>
    {trend}
    {table}
    <div style="height:28px"></div>"""


def build_financial_html(rows: list[dict], brand: str = "Your Report",
                         branding: Branding | None = None,
                         insight_text: str = "",
                         title: str = "Quarterly Fundamentals Report") -> str:
    companies = load_fundamentals(rows)
    if branding is None:
        branding = Branding(brand=brand)

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

    latest = max(c.latest.fiscal_date for c in companies)
    sections = "".join(_company_section(c) for c in companies)
    e_brand = _html.escape(branding.brand)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e_brand} — {_html.escape(title)}</title>
{_extract_style()}
</head><body><div class="wrap">
  <header>
    <div class="header-row">
      <div class="header-main">
        <div class="eyebrow">{_html.escape(title)}</div>
        <h1>{e_brand}</h1>
        <div class="range">{len(companies)} compan{'ies' if len(companies) != 1 else 'y'} · latest quarter {_html.escape(latest)}</div>
      </div>
      {logo_html}
    </div>
    {contact_html}
  </header>
  <div class="body">
    {insight_html}
    {sections}
  </div>
  <footer>
    <span>Prepared for {e_brand}</span>
    <span>Generated by GremlinHunter Reporting</span>
  </footer>
</div></body></html>"""


def write_financial_html(rows, path, brand="Your Report", branding=None,
                         insight_text="", title="Quarterly Fundamentals Report"):
    html = build_financial_html(rows, brand, branding, insight_text, title)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
