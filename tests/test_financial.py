"""Financial lane: connector -> normalized CSV -> router -> fixed-format report,
with the payload feeding both insight and verification."""
import os
import csv

from connectors.fundamentals import fixture_fetch_fn, FIELDNAMES, normalize_statement
from engine.financial_report import (load_fundamentals, financial_metrics_payload,
                                     looks_like_fundamentals, build_financial_html,
                                     FundamentalsError)
from engine.insight import deterministic_financial_summary, generate_financial_insight
from engine.router import decide
from engine.profile import load_csv_rows


def _land_fixture(tmp_path, tickers=("NVAX", "MERD"), quarters=8):
    dest = str(tmp_path / "fundamentals.csv")
    fixture_fetch_fn("unused-key", {"tickers": list(tickers), "quarters": quarters}, dest)
    return dest


# --------------------------- connector contract --------------------------- #
def test_connector_lands_normalized_csv(tmp_path):
    dest = _land_fixture(tmp_path)
    with open(dest, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows and list(rows[0].keys()) == FIELDNAMES
    assert {r["ticker"] for r in rows} == {"NVAX", "MERD"}
    assert len(rows) == 16                     # 2 tickers × 8 quarters


def test_connector_honors_ticker_filter_and_quarter_cap(tmp_path):
    dest = str(tmp_path / "one.csv")
    fixture_fetch_fn("k", {"tickers": ["NVAX"], "quarters": 4}, dest)
    rows = list(csv.DictReader(open(dest, newline="", encoding="utf-8")))
    assert len(rows) == 4 and all(r["ticker"] == "NVAX" for r in rows)


def test_normalize_computes_margins():
    row = normalize_statement("test", {"date": "2026-03-31", "period": "Q1",
                                       "calendarYear": "2026", "revenue": 1000,
                                       "grossProfit": 600, "operatingIncome": 250,
                                       "netIncome": 200, "eps": 1.5})
    assert row["gross_margin_pct"] == 60.0
    assert row["net_margin_pct"] == 20.0


# ------------------------------ metrics layer ----------------------------- #
def test_load_fundamentals_and_growth(tmp_path):
    rows = load_csv_rows(_land_fixture(tmp_path))
    companies = load_fundamentals(rows)
    nvax = next(c for c in companies if c.ticker == "NVAX")
    assert len(nvax.quarters) == 8
    assert nvax.latest.fiscal_date == "2026-03-31"
    # fixture: Q1'26 rev 1,842M vs Q1'25 1,398M -> +31.8% YoY
    assert abs(nvax.revenue_yoy_pct - 31.8) < 0.15
    assert nvax.revenue_qoq_pct > 0


def test_payload_carries_market_context(tmp_path):
    rows = load_csv_rows(_land_fixture(tmp_path))
    p = financial_metrics_payload(load_fundamentals(rows),
                                  market_context="rate cuts priced in for H2")
    assert p["market_context"] == "rate cuts priced in for H2"
    assert len(p["companies"]) == 2


def test_too_thin_data_rejected():
    try:
        load_fundamentals([{"ticker": "X", "fiscal_date": "2026-01-01",
                            "revenue": "100"}])
        assert False, "one quarter should not be reportable"
    except FundamentalsError:
        pass


# --------------------------------- router --------------------------------- #
def test_router_sniffs_and_honors_declaration():
    fin_headers = FIELDNAMES
    assert looks_like_fundamentals(fin_headers)
    assert decide("financial", fin_headers)[0] == "financial"
    assert decide("auto", fin_headers)[0] == "financial"
    # declared financial on non-financial data falls back, never breaks
    rendered, note = decide("financial", ["name", "amount"])
    assert rendered == "generic" and "weren't found" in note


# -------------------------------- template -------------------------------- #
def test_fixed_format_renders_every_company_same_shape(tmp_path):
    rows = load_csv_rows(_land_fixture(tmp_path))
    html = build_financial_html(rows, brand="Pilot Client")
    for t in ("NVAX", "MERD"):
        assert f"{t} — Quarterly fundamentals" in html
    assert html.count("Revenue by quarter") == 2      # same section per company
    assert "Pilot Client" in html


# ------------------------- insight + verification ------------------------- #
def test_deterministic_financial_summary_is_grounded(tmp_path):
    rows = load_csv_rows(_land_fixture(tmp_path))
    payload = financial_metrics_payload(load_fundamentals(rows))
    text = deterministic_financial_summary(payload)
    assert "NVAX" in text and "MERD" in text
    from engine.verify import verify_text
    assert verify_text(text, payload).ok      # our own fallback must verify


def test_financial_insight_gates_closed_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rows = load_csv_rows(_land_fixture(tmp_path))
    payload = financial_metrics_payload(load_fundamentals(rows))
    # opted out -> deterministic; opted in with no key -> deterministic. Never raises.
    assert generate_financial_insight(payload) == deterministic_financial_summary(payload)
    assert generate_financial_insight(payload, ai_opted_in=True) \
        == deterministic_financial_summary(payload)


def test_hallucinated_insight_is_discarded(tmp_path, monkeypatch):
    """The whole point: a model that invents a number never reaches the client."""
    rows = load_csv_rows(_land_fixture(tmp_path))
    payload = financial_metrics_payload(load_fundamentals(rows))

    class _Block:
        type = "text"
        text = "NVAX grew revenue 87% YoY to $9.9B on soaring demand."

    class _Msg:
        content = [_Block()]

    class _Messages:
        def create(self, **kw):
            return _Msg()

    class _FakeClient:
        messages = _Messages()

    out = generate_financial_insight(payload, ai_opted_in=True, client=_FakeClient())
    assert out == deterministic_financial_summary(payload)
