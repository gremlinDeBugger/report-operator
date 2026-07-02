"""
report.py — Builds the HTML dashboard (and a print/PDF-ready version) from a
Report produced by analytics.py.

Design: a clean, confident performance dashboard. Dark instrument-panel header
with the headline KPIs, then a light analytical body — daily spend/ROAS trend,
a sortable campaign table, and call-outs for best/worst performers. Built to
read like a deliverable an agency hands a client, not a generic admin panel.

Author: Jared Jowett
"""
from __future__ import annotations

import html as _html
import os
import base64
import mimetypes
from dataclasses import dataclass
try:
    from engine.analytics import Report, load_report   # when used as a package
except ImportError:
    from analytics import Report, load_report           # when run standalone in engine/


@dataclass
class Branding:
    """Per-client branding placed on the report. All optional except brand."""
    brand: str = "Your Brand"
    business_name: str = ""
    email: str = ""
    phone: str = ""
    logo_path: str = ""        # path to a png/jpg/svg; embedded as data-URI at render

    def logo_data_uri(self) -> str:
        """Return the logo as a self-contained data: URI, or '' if none/unreadable.
        Embedding (rather than linking) keeps the report a single portable file."""
        if not self.logo_path or not os.path.exists(self.logo_path):
            return ""
        mime, _ = mimetypes.guess_type(self.logo_path)
        if not mime or not mime.startswith("image/"):
            return ""
        try:
            with open(self.logo_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return f"data:{mime};base64,{b64}"
        except OSError:
            return ""


def _fmt_reach(reach: int) -> str:
    if reach >= 1_000_000:
        return f"{reach / 1_000_000:.2f}M"
    if reach >= 1_000:
        return f"{reach / 1_000:.1f}K"
    return f"{reach:,}"


def _spark_points(values, width=760, height=120, pad=6):
    """Return SVG polyline points scaled to the box."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (i / (n - 1 if n > 1 else 1)) * (width - 2 * pad)
        y = height - pad - ((v - lo) / rng) * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def build_html(report: Report, brand: str = "Your Brand",
               branding: "Branding | None" = None,
               insight_text: str = "") -> str:
    # branding takes precedence; fall back to a bare brand string for the basic lane
    if branding is None:
        branding = Branding(brand=brand)
    o = report.overall
    days = list(report.by_day.keys())
    spend_series = [m.spend for m in report.by_day.values()]
    roas_series = [m.roas for m in report.by_day.values()]

    # campaign rows sorted by spend desc
    camp_rows = sorted(report.by_campaign.items(), key=lambda kv: kv[1].spend, reverse=True)

    best = report.best_roas_campaign
    worst = report.worst_roas_campaign

    spend_poly = _spark_points(spend_series)
    roas_poly = _spark_points(roas_series)

    e_brand = _html.escape(branding.brand)
    e_date0 = _html.escape(report.date_range[0])
    e_date1 = _html.escape(report.date_range[1])

    # --- optional branding fragments: each renders ONLY if provided --- #
    _logo_uri = branding.logo_data_uri()
    logo_html = (f'<img class="brand-logo" src="{_logo_uri}" alt="">'
                 if _logo_uri else "")

    _contact_bits = []
    if branding.business_name:
        _contact_bits.append(f'<span class="biz">{_html.escape(branding.business_name)}</span>')
    if branding.email:
        _contact_bits.append(_html.escape(branding.email))
    if branding.phone:
        _contact_bits.append(_html.escape(branding.phone))
    contact_html = (f'<div class="contact">{" · ".join(_contact_bits)}</div>'
                    if _contact_bits else "")

    # optional AI / analyst insight block — omitted entirely if empty
    insight_html = (f'<div class="insight"><div class="insight-tag">Analysis</div>'
                    f'<p>{_html.escape(insight_text)}</p></div>'
                    if insight_text else "")

    def kpi(label, value, sub=""):
        return f"""
        <div class="kpi">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-sub">{sub}</div>
        </div>"""

    kpis = "".join([
        kpi("Amount spent", f"${o.spend:,.0f}", f"{e_date0} – {e_date1}"),
        kpi("ROAS", f"{o.roas:.2f}×", f"${o.conv_value:,.0f} value returned"),
        kpi("Conversions", f"{o.conversions:,}", f"${o.cpa:,.2f} cost per result"),
        kpi("Reach", _fmt_reach(o.reach), f"{o.frequency:.1f}× avg frequency"),
        kpi("Link CTR", f"{o.ctr:.2f}%", f"${o.cpc:.2f} cost per click"),
        kpi("Impressions", _fmt_reach(o.impressions), f"${o.cpm:.2f} CPM"),
    ])

    rows_html = ""
    max_spend = max((m.spend for _, m in camp_rows), default=1)
    for name, m in camp_rows:
        obj = report.objectives.get(name, "")
        e_name = _html.escape(name)
        e_obj = _html.escape(obj)
        bar = (m.spend / max_spend * 100) if max_spend else 0
        roas_class = "good" if m.roas >= o.roas else "weak"
        rows_html += f"""
        <tr>
          <td class="c-name"><span class="dot"></span>{e_name}<span class="obj">{e_obj}</span></td>
          <td class="num"><div class="bar-wrap"><div class="bar" style="width:{bar:.0f}%"></div></div>${m.spend:,.0f}</td>
          <td class="num">{m.ctr:.2f}%</td>
          <td class="num">${m.cpc:.2f}</td>
          <td class="num">{m.conversions:,}</td>
          <td class="num">${m.cpa:,.2f}</td>
          <td class="num roas {roas_class}">{m.roas:.2f}×</td>
        </tr>"""

    callouts = ""
    if best:
        e_best_name = _html.escape(best[0])
        callouts += f"""
        <div class="callout up">
          <div class="co-tag">Top performer</div>
          <div class="co-name">{e_best_name}</div>
          <div class="co-metric">{best[1].roas:.2f}× ROAS · ${best[1].spend:,.0f} spent</div>
        </div>"""
    if worst and worst[0] != (best[0] if best else None):
        e_worst_name = _html.escape(worst[0])
        callouts += f"""
        <div class="callout down">
          <div class="co-tag">Needs attention</div>
          <div class="co-name">{e_worst_name}</div>
          <div class="co-metric">{worst[1].roas:.2f}× ROAS · ${worst[1].spend:,.0f} spent</div>
        </div>"""

    # max for axis labels
    spend_hi = max(spend_series) if spend_series else 0
    roas_hi = max(roas_series) if roas_series else 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e_brand} — Meta Ads Performance Report</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap');
  :root {{
    --ink:#0f1320; --panel:#161b2e; --line:#e6e8ef; --muted:#7c8398;
    --accent:#4f7cff; --good:#1faa6c; --weak:#e0663b; --bg:#f6f7fb;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Inter',system-ui,sans-serif; color:var(--ink); background:var(--bg); }}
  .wrap {{ max-width:980px; margin:0 auto; padding:0 0 60px; }}

  header {{ background:var(--ink); color:#fff; padding:34px 40px 30px; }}
  .eyebrow {{ font-family:'Space Grotesk'; font-size:12px; letter-spacing:.18em;
    text-transform:uppercase; color:#8fa3d8; font-weight:600; }}
  header h1 {{ font-family:'Space Grotesk'; font-weight:700; font-size:30px;
    margin-top:6px; letter-spacing:-.01em; }}
  header .range {{ color:#aeb6cc; font-size:14px; margin-top:4px; }}
  .header-row {{ display:flex; justify-content:space-between; align-items:flex-start; gap:20px; }}
  .header-main {{ min-width:0; }}
  .brand-logo {{ max-height:54px; max-width:200px; width:auto; object-fit:contain;
    flex:0 0 auto; border-radius:6px; background:#fff; padding:6px 10px; }}
  .contact {{ color:#aeb6cc; font-size:12.5px; margin-top:14px;
    border-top:1px solid #2a3150; padding-top:12px; }}
  .contact .biz {{ color:#fff; font-weight:600; }}
  .insight {{ background:#fff; border:1px solid var(--line); border-left:4px solid var(--accent);
    border-radius:12px; padding:18px 22px; margin-bottom:26px; }}
  .insight-tag {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase;
    font-weight:600; color:var(--accent); margin-bottom:7px; }}
  .insight p {{ font-size:14.5px; line-height:1.55; color:#2a3045; }}

  .kpis {{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
    background:#222842; }}
  .kpi {{ background:var(--panel); padding:20px 22px; }}
  .kpi-label {{ font-size:11px; letter-spacing:.12em; text-transform:uppercase;
    color:#8a93ad; font-weight:600; }}
  .kpi-value {{ font-family:'Space Grotesk'; font-size:30px; font-weight:700;
    color:#fff; margin-top:6px; line-height:1; }}
  .kpi-sub {{ font-size:12.5px; color:#9aa3bd; margin-top:7px; }}

  .body {{ padding:32px 40px; }}
  .section-title {{ font-family:'Space Grotesk'; font-size:13px; font-weight:600;
    letter-spacing:.1em; text-transform:uppercase; color:var(--muted);
    margin:6px 0 14px; }}

  .chart-card {{ background:#fff; border:1px solid var(--line); border-radius:14px;
    padding:22px 24px 16px; margin-bottom:26px; }}
  .chart-head {{ display:flex; justify-content:space-between; align-items:baseline;
    margin-bottom:8px; }}
  .chart-head h3 {{ font-family:'Space Grotesk'; font-size:16px; font-weight:600; }}
  .legend {{ font-size:12px; color:var(--muted); }}
  .legend b {{ color:var(--accent); }} .legend i {{ color:var(--good); font-style:normal; }}
  svg {{ width:100%; height:auto; display:block; }}
  .axis {{ font-size:10px; fill:var(--muted); font-family:'Inter'; }}

  .callouts {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:26px; }}
  .callout {{ border:1px solid var(--line); border-radius:12px; padding:16px 18px; background:#fff;
    border-left-width:4px; }}
  .callout.up {{ border-left-color:var(--good); }}
  .callout.down {{ border-left-color:var(--weak); }}
  .co-tag {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; font-weight:600; color:var(--muted); }}
  .co-name {{ font-family:'Space Grotesk'; font-size:17px; font-weight:600; margin:4px 0 2px; }}
  .co-metric {{ font-size:13px; color:#4a5168; }}

  table {{ width:100%; border-collapse:collapse; background:#fff;
    border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
  thead th {{ font-size:11px; letter-spacing:.06em; text-transform:uppercase;
    color:var(--muted); font-weight:600; text-align:right; padding:13px 14px;
    background:#fbfbfd; border-bottom:1px solid var(--line); }}
  thead th:first-child {{ text-align:left; }}
  tbody td {{ padding:13px 14px; font-size:13.5px; border-bottom:1px solid #f0f1f6; text-align:right; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .c-name {{ text-align:left; font-weight:500; position:relative; }}
  .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%;
    background:var(--accent); margin-right:9px; }}
  .obj {{ display:block; font-size:11px; color:var(--muted); margin-left:17px; margin-top:1px; }}
  .num {{ font-variant-numeric:tabular-nums; }}
  .bar-wrap {{ display:inline-block; width:46px; height:5px; background:#eef0f6;
    border-radius:3px; margin-right:8px; vertical-align:middle; overflow:hidden; }}
  .bar {{ height:100%; background:var(--accent); border-radius:3px; }}
  .roas {{ font-weight:600; }}
  .roas.good {{ color:var(--good); }} .roas.weak {{ color:var(--weak); }}

  footer {{ padding:22px 40px; color:var(--muted); font-size:12px;
    display:flex; justify-content:space-between; border-top:1px solid var(--line); margin-top:8px; }}
  @media print {{ body {{ background:#fff; }} .chart-card,table,.callout {{ break-inside:avoid; }} }}
  @media (max-width:680px) {{ .kpis{{grid-template-columns:repeat(2,1fr);}} .callouts{{grid-template-columns:1fr;}}
    header,.body,footer{{padding-left:20px;padding-right:20px;}} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="header-row">
      <div class="header-main">
        <div class="eyebrow">Meta Ads · Performance Report</div>
        <h1>{e_brand}</h1>
        <div class="range">Reporting period {e_date0} to {e_date1} · {len(report.by_campaign)} campaigns</div>
      </div>
      {logo_html}
    </div>
    {contact_html}
  </header>

  <div class="kpis">{kpis}</div>

  <div class="body">
    {insight_html}
    <div class="section-title">Daily trend</div>
    <div class="chart-card">
      <div class="chart-head">
        <h3>Spend &amp; ROAS over time</h3>
        <div class="legend"><b>● Spend</b> &nbsp; <i>● ROAS</i></div>
      </div>
      <svg viewBox="0 0 760 150" preserveAspectRatio="none">
        <polyline fill="none" stroke="#4f7cff" stroke-width="2.5" points="{spend_poly}"/>
        <polyline fill="none" stroke="#1faa6c" stroke-width="2.5" stroke-dasharray="0"
          points="{_spark_points(roas_series)}"/>
        <text class="axis" x="6" y="12">spend peak ${spend_hi:,.0f}</text>
        <text class="axis" x="570" y="12" style="fill:#1faa6c">ROAS peak {roas_hi:.2f}×</text>
        <text class="axis" x="6" y="146">{e_date0}</text>
        <text class="axis" x="690" y="146">{e_date1}</text>
      </svg>
    </div>

    <div class="section-title">Performance highlights</div>
    <div class="callouts">{callouts}</div>

    <div class="section-title">Campaign breakdown</div>
    <table>
      <thead><tr>
        <th>Campaign</th><th>Spend</th><th>CTR</th><th>CPC</th>
        <th>Results</th><th>CPA</th><th>ROAS</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <footer>
    <span>Prepared for {e_brand}</span>
    <span>Generated by GremlinHunter Reporting · Meta Ads</span>
  </footer>
</div>
</body>
</html>"""


def write_html(report: Report, path: str, brand: str = "Your Brand",
               branding: "Branding | None" = None, insight_text: str = ""):
    html = build_html(report, brand, branding, insight_text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


if __name__ == "__main__":
    r = load_report("sample_fb_export.csv")
    write_html(r, "sample_output/report.html", brand="Northwind Outfitters")
    print("wrote sample_output/report.html")
