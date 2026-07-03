"""
runner.py — Produces a report, scoped to one job.

This is where the two customer lanes are kept apart, by design:

    run_csv(csv_path, brand, out_dir)
        BASIC lane. A walk-in customer hands you a CSV; you hand back a report.
        Imports ONLY the engine. Does NOT import registry, does NOT decrypt
        anything, does NOT know the scheduler exists. If the entire keyed/
        custodial system were deleted, this function would still work.

    run_client(registry, client_id, out_root)
        KEYED lane. A custodial client with a stored key, run by id. Pulls the
        client's key from the registry, runs the engine scoped to that client,
        writes to that client's own output folder. One client's failure is
        contained to that client.

The shared piece is the engine (engine.analytics + engine.report) — the part
that actually builds the report. Everything that distinguishes the two lanes
(keys, scheduling, isolation) lives ABOVE the engine, not inside it.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass

# NOTE: this module imports the engine only. It does NOT import registry at
# module top-level — the keyed lane pulls it in locally so the basic CSV path
# has zero dependency on the credential machinery.
from engine.analytics import load_report, ReportError
from engine.report import write_html
from engine.insight import generate_insight

log = logging.getLogger("runner")


@dataclass
class RunResult:
    ok: bool
    client_id: str | None
    brand: str
    html_path: str | None = None
    pdf_path: str | None = None
    error: str | None = None


def _safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s).strip("_") or "client"


def _build(csv_path: str, brand: str, out_dir: str, client_id: str | None,
           ai_opted_in: bool = False, report_type: str = "auto",
           market_context: str | dict | None = None) -> RunResult:
    """Shared build step. Used by BOTH lanes — but only ever with a CSV already
    on disk. The keyed lane is responsible for producing that CSV from its live
    pull first; the basic lane is handed one directly. Routes by declared
    report_type (meta/financial/generic) exactly like the basic lane."""
    os.makedirs(out_dir, exist_ok=True)

    from engine.router import decide
    from engine.profile import load_csv_rows
    rows = load_csv_rows(csv_path)
    headers = list(rows[0].keys()) if rows else []
    rendered_as, _note = decide(report_type, headers)

    if rendered_as == "financial":
        from engine.financial_report import (load_fundamentals,
                                             financial_metrics_payload,
                                             write_financial_html)
        from engine.insight import generate_financial_insight
        try:
            companies = load_fundamentals(rows)
        except Exception as e:
            log.error("report build failed for %s: %s", brand, e)
            return RunResult(ok=False, client_id=client_id, brand=brand, error=str(e))
        payload = financial_metrics_payload(companies, market_context=market_context)
        insight_text = generate_financial_insight(payload, ai_opted_in=ai_opted_in,
                                                  client_id=client_id)
        html_path = os.path.join(out_dir, "report.html")
        write_financial_html(rows, html_path, brand=brand, insight_text=insight_text)
    else:
        try:
            report = load_report(csv_path)
        except ReportError as e:
            log.error("report build failed for %s: %s", brand, e)
            return RunResult(ok=False, client_id=client_id, brand=brand, error=str(e))

        # AI insight is opt-in; generate_insight always returns *something* (AI or
        # deterministic fallback), so the report never fails on this.
        insight_text = generate_insight(report, ai_opted_in=ai_opted_in, client_id=client_id)

        html_path = os.path.join(out_dir, "report.html")
        write_html(report, html_path, brand=brand, insight_text=insight_text)

    pdf_path = None
    try:
        from engine.make_pdf import html_to_pdf
        pdf_path = os.path.join(out_dir, "report.pdf")
        html_to_pdf(os.path.abspath(html_path), os.path.abspath(pdf_path))
    except Exception as e:
        log.warning("PDF skipped for %s (%s) — HTML still produced", brand, e)
        pdf_path = None

    return RunResult(ok=True, client_id=client_id, brand=brand,
                     html_path=html_path, pdf_path=pdf_path)


# --------------------------------------------------------------------------- #
# BASIC lane — no keys, no registry, no scheduler.
# --------------------------------------------------------------------------- #
def run_csv(csv_path: str, brand: str = "Your Brand",
            out_dir: str = "output", ai_insight: bool = False,
            report_type: str = "auto",
            market_context: str | dict | None = None) -> RunResult:
    """
    Basic walk-in job: CSV in, report out. This is the Fiverr $20 lane.
    Deliberately has no access to the credential store — a customer here can
    never be confused with, or affected by, a keyed client.

    report_type: the customer can declare what their data is (auto|generic|meta|
    sales|catalog|survey); routes to the Meta template or the generic engine.
    ai_insight: a paid add-on even here — a no-key walk-in can still get the AI
    write-up (paid by the operator key); insight runs on the profile/metrics and
    needs no Meta key of any kind.
    """
    if not os.path.exists(csv_path):
        return RunResult(ok=False, client_id=None, brand=brand,
                         error=f"CSV not found: {csv_path}")
    log.info("[basic] report for '%s' from %s (type=%s ai=%s)",
             brand, csv_path, report_type, ai_insight)

    from engine.router import render as route_render, decide
    from engine.report import Branding
    from engine.profile import profile_data, load_csv_rows
    from engine.generic_report import summarize
    from engine.insight import generate_generic_insight
    try:
        rows = load_csv_rows(csv_path)
        if not rows:
            return RunResult(ok=False, client_id=None, brand=brand,
                             error="empty CSV (no data rows)")
        rendered_as, _ = decide(report_type, list(rows[0].keys()))
        if rendered_as == "meta":
            insight_text = generate_insight(load_report(csv_path), ai_opted_in=ai_insight)
        elif rendered_as == "financial":
            from engine.financial_report import load_fundamentals, financial_metrics_payload
            from engine.insight import generate_financial_insight
            payload = financial_metrics_payload(load_fundamentals(rows),
                                                market_context=market_context)
            insight_text = generate_financial_insight(payload, ai_opted_in=ai_insight)
        else:
            prof = profile_data(rows)
            insight_text = generate_generic_insight(
                prof, summarize(rows, prof), ai_opted_in=ai_insight,
                report_type=report_type)
        routed = route_render(csv_path, out_dir, report_type=report_type,
                              brand=brand, branding=Branding(brand=brand),
                              insight_text=insight_text, file_stem="report")
        return RunResult(ok=True, client_id=None, brand=brand,
                         html_path=routed.html_path, pdf_path=routed.pdf_path)
    except Exception as e:
        log.error("[basic] failed: %s", e)
        return RunResult(ok=False, client_id=None, brand=brand, error=str(e))


# --------------------------------------------------------------------------- #
# KEYED lane — registry-backed, isolated per client.
# --------------------------------------------------------------------------- #
def run_client(registry, client_id: str, out_root: str = "client_output",
               fetch_fn=None) -> RunResult:
    """
    Run one keyed client by id. Looks up their key, produces their data CSV via
    fetch_fn (the live-API pull — injected so this stays testable and so the
    Meta connector can be swapped in without touching this file), then builds
    the report scoped to that client's own folder.

    `fetch_fn(api_key, report_config, dest_csv) -> None` is the only thing that
    differs between "live Meta pull" and a test stub. Until the real Meta
    connector is bolted on, a stub supplies the CSV.
    """
    try:
        client = registry.get(client_id)
    except Exception as e:
        return RunResult(ok=False, client_id=client_id, brand="?", error=str(e))

    if not client.active:
        return RunResult(ok=False, client_id=client_id, brand=client.brand,
                         error=f"client '{client_id}' is revoked/inactive")

    out_dir = os.path.join(out_root, _safe_name(client_id))
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "_data.csv")

    # Decrypt the key ONLY here, ONLY for this client, and let it fall out of
    # scope as soon as the fetch is done.
    try:
        api_key = registry.decrypt_key_for(client_id)
    except Exception as e:
        return RunResult(ok=False, client_id=client_id, brand=client.brand, error=str(e))

    try:
        if fetch_fn is None:
            raise RuntimeError(
                "no fetch_fn supplied — the live Meta connector isn't bolted on "
                "yet. Inject a fetch_fn(api_key, report_config, dest_csv)."
            )
        fetch_fn(api_key, client.report_config, csv_path)
    except Exception as e:
        log.error("[keyed] data fetch failed for '%s': %s", client_id, e)
        return RunResult(ok=False, client_id=client_id, brand=client.brand,
                         error=f"fetch failed: {e}")
    finally:
        api_key = None   # don't keep the plaintext key around

    log.info("[keyed] building report for client '%s' (%s)", client_id, client.brand)
    rc = client.report_config or {}
    declared = rc.get("report_type") or getattr(client, "report_type", "auto") or "auto"
    result = _build(csv_path, client.brand, out_dir, client_id=client_id,
                    ai_opted_in=client.ai_insight,
                    report_type=declared,
                    market_context=rc.get("market_context"))
    if result.ok:
        registry.mark_run(client_id)
    return result
