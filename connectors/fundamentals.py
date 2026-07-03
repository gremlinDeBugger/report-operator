"""
fundamentals.py — Live financial-data connector for the keyed lane.

Implements the runner's one seam, exactly:

    fetch_fn(api_key, report_config, dest_csv) -> None

Pulls quarterly income statements for a list of tickers from Financial Modeling
Prep (FMP) and lands them as ONE normalized CSV — one row per company-quarter —
which the financial report template and the profiler both read natively. The
rest of the system (registry, scheduler, router, insight, verification) does not
know or care that the data came from a live API.

report_config keys used:
    tickers   : ["AAPL", "MSFT", ...]     required
    quarters  : 8                          optional, default 8, max 40
    base_url  : override for testing       optional

Normalized columns (stable contract — the template keys off these):
    ticker, fiscal_date, period, calendar_year,
    revenue, gross_profit, operating_income, net_income, eps,
    gross_margin_pct, operating_margin_pct, net_margin_pct

Errors RAISE (network down, bad key, unknown ticker with no data at all):
runner.run_client() already catches per-client, so one client's bad key can't
take down the book. A ticker that returns no rows is skipped with a log line;
the fetch only fails if NO ticker produced data.

Offline mode: fixture_fetch_fn() serves the same contract from a bundled JSON
fixture — used by tests and demos, zero network.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import csv
import json
import logging
import urllib.request
import urllib.parse

log = logging.getLogger("connector.fundamentals")

DEFAULT_BASE = "https://financialmodelingprep.com/stable"
FIELDNAMES = ["ticker", "fiscal_date", "period", "calendar_year",
              "revenue", "gross_profit", "operating_income", "net_income", "eps",
              "gross_margin_pct", "operating_margin_pct", "net_margin_pct"]

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "sample_fundamentals.json")


# --------------------------------------------------------------------------- #
# Normalization — provider JSON -> our stable row contract
# --------------------------------------------------------------------------- #
def _pct(part, whole) -> float | None:
    try:
        if whole in (None, 0):
            return None
        return round(float(part) / float(whole) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def normalize_statement(ticker: str, stmt: dict) -> dict:
    """One FMP income-statement object -> one normalized CSV row."""
    revenue = stmt.get("revenue")
    row = {
        "ticker": ticker.upper(),
        "fiscal_date": stmt.get("date", ""),
        "period": stmt.get("period", ""),
        "calendar_year": stmt.get("calendarYear") or stmt.get("fiscalYear", ""),
        "revenue": revenue,
        "gross_profit": stmt.get("grossProfit"),
        "operating_income": stmt.get("operatingIncome"),
        "net_income": stmt.get("netIncome"),
        "eps": stmt.get("eps"),
        "gross_margin_pct": _pct(stmt.get("grossProfit"), revenue),
        "operating_margin_pct": _pct(stmt.get("operatingIncome"), revenue),
        "net_margin_pct": _pct(stmt.get("netIncome"), revenue),
    }
    return row


def _write_rows(rows: list[dict], dest_csv: str):
    os.makedirs(os.path.dirname(os.path.abspath(dest_csv)), exist_ok=True)
    # newest-first from the API; the template sorts anyway, but keep the file
    # readable: group by ticker, oldest -> newest
    rows.sort(key=lambda r: (r["ticker"], r["fiscal_date"]))
    with open(dest_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------- #
# The live fetch_fn
# --------------------------------------------------------------------------- #
def fetch_quarterly_fundamentals(api_key: str, report_config: dict,
                                 dest_csv: str) -> None:
    """
    THE fetch_fn for financial clients. Plugs straight into
    runner.run_client(..., fetch_fn=fetch_quarterly_fundamentals).
    """
    cfg = report_config or {}
    tickers = [t.strip().upper() for t in cfg.get("tickers", []) if t.strip()]
    if not tickers:
        raise ValueError("report_config.tickers is empty — nothing to fetch")
    quarters = max(1, min(int(cfg.get("quarters", 8)), 40))
    base = cfg.get("base_url", DEFAULT_BASE).rstrip("/")

    rows: list[dict] = []
    for t in tickers:
        url = (f"{base}/income-statement?symbol={urllib.parse.quote(t)}"
               f"&period=quarter&limit={quarters}&apikey={urllib.parse.quote(api_key)}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "report-operator/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            # a single bad ticker shouldn't sink the batch — log and continue,
            # unless it smells like a key problem, which will fail every ticker
            log.warning("fetch failed for %s: %s", t, e)
            continue
        if isinstance(data, dict) and data.get("Error Message"):
            raise RuntimeError(f"provider rejected the request: {data['Error Message']}")
        if not isinstance(data, list) or not data:
            log.warning("no data returned for %s — skipped", t)
            continue
        rows.extend(normalize_statement(t, s) for s in data)

    if not rows:
        raise RuntimeError(f"no fundamentals returned for any of: {', '.join(tickers)}")
    _write_rows(rows, dest_csv)
    log.info("landed %d company-quarters for %d tickers -> %s",
             len(rows), len(tickers), dest_csv)


# --------------------------------------------------------------------------- #
# Offline fixture — same contract, zero network (tests + demos)
# --------------------------------------------------------------------------- #
def fixture_fetch_fn(api_key: str, report_config: dict, dest_csv: str) -> None:
    """Drop-in fetch_fn that serves bundled sample data. Honors the same
    report_config (tickers filter + quarters cap) so demos behave like live."""
    with open(_FIXTURE, encoding="utf-8") as f:
        fixture = json.load(f)
    cfg = report_config or {}
    wanted = {t.strip().upper() for t in cfg.get("tickers", [])} or set(fixture)
    quarters = max(1, min(int(cfg.get("quarters", 8)), 40))

    rows: list[dict] = []
    for ticker, stmts in fixture.items():
        if ticker.upper() not in wanted:
            continue
        rows.extend(normalize_statement(ticker, s) for s in stmts[:quarters])
    if not rows:
        raise RuntimeError(f"fixture has no data for: {', '.join(sorted(wanted))}")
    _write_rows(rows, dest_csv)


# --------------------------------------------------------------------------- #
# Alpha Vantage — second provider, same contract (free tier includes quarterly)
# --------------------------------------------------------------------------- #
AV_BASE = "https://www.alphavantage.co/query"

def _av_num(v):
    try:
        return None if v in (None, "None", "") else float(v)
    except (TypeError, ValueError):
        return None

def _av_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "report-operator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_quarterly_fundamentals_av(api_key: str, report_config: dict,
                                    dest_csv: str) -> None:
    """fetch_fn for Alpha Vantage. Quarterly income statements + EPS from the
    earnings endpoint, merged by fiscal date, normalized to the same CSV
    contract as the FMP connector. Free tier: ~25 requests/day (2 per ticker)."""
    cfg = report_config or {}
    tickers = [t.strip().upper() for t in cfg.get("tickers", []) if t.strip()]
    if not tickers:
        raise ValueError("report_config.tickers is empty — nothing to fetch")
    quarters = max(1, min(int(cfg.get("quarters", 8)), 40))

    rows = []
    for t in tickers:
        try:
            inc = _av_get(f"{AV_BASE}?function=INCOME_STATEMENT&symbol={urllib.parse.quote(t)}&apikey={urllib.parse.quote(api_key)}")
            earn = _av_get(f"{AV_BASE}?function=EARNINGS&symbol={urllib.parse.quote(t)}&apikey={urllib.parse.quote(api_key)}")
        except Exception as e:
            log.warning("fetch failed for %s: %s", t, e)
            continue
        note = inc.get("Note") or inc.get("Information") or inc.get("Error Message")
        if note:
            raise RuntimeError(f"provider said: {note}")
        eps_by_date = {q.get("fiscalDateEnding"): _av_num(q.get("reportedEPS"))
                       for q in (earn.get("quarterlyEarnings") or [])}
        for stmt in (inc.get("quarterlyReports") or [])[:quarters]:
            d = stmt.get("fiscalDateEnding", "")
            revenue = _av_num(stmt.get("totalRevenue"))
            gp = _av_num(stmt.get("grossProfit"))
            oi = _av_num(stmt.get("operatingIncome"))
            ni = _av_num(stmt.get("netIncome"))
            qtr = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}.get(
                int(d[5:7]) if len(d) >= 7 else 0, "")
            rows.append({
                "ticker": t, "fiscal_date": d, "period": qtr,
                "calendar_year": d[:4],
                "revenue": revenue, "gross_profit": gp,
                "operating_income": oi, "net_income": ni,
                "eps": eps_by_date.get(d),
                "gross_margin_pct": _pct(gp, revenue),
                "operating_margin_pct": _pct(oi, revenue),
                "net_margin_pct": _pct(ni, revenue),
            })
    if not rows:
        raise RuntimeError(f"no fundamentals returned for any of: {', '.join(tickers)}")
    _write_rows(rows, dest_csv)
    log.info("landed %d company-quarters from Alpha Vantage -> %s", len(rows), dest_csv)
