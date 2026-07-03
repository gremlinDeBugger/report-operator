"""
insight.py — Optional AI-written summary for a report, with a deterministic
fallback that always works.

The insight is computed from the report's METRICS (not raw rows), so the prompt
is tiny and the cost per call is ~$0.002 on Haiku. It is paid by the OPERATOR's
single ANTHROPIC_API_KEY — customers never supply an AI key.

It runs for BOTH lanes (live-pull keyed clients AND basic CSV walk-ins): the
insight only needs the computed metrics, which exist regardless of where the data
came from. A customer with no Meta key can still receive an AI report.

A report gets the AI write-up only if ALL hold; otherwise it falls back to a
deterministic number-based summary and the report still generates:
    1. the customer opted in (ai_insight=True)
    2. the customer's own ceiling (if they set one) isn't exceeded
    3. the global circuit breaker hasn't tripped
    4. the API key is present and the call succeeds

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import json
import logging
from datetime import date

log = logging.getLogger("insight")

MODEL = os.environ.get("INSIGHT_MODEL", "claude-haiku-4-5-20251001")
API_KEY_ENV = "ANTHROPIC_API_KEY"

# Global circuit breaker. At ~$0.002/call, 50,000 calls ≈ $100. This protects
# the operator key from a runaway loop/bug — it is NOT a budget you manage.
GLOBAL_MONTHLY_CALL_CEILING = int(os.environ.get("INSIGHT_GLOBAL_CEILING", "50000"))

# Tiny JSON usage ledger next to the data store. Tracks calls-per-month so the
# breaker (and customer ceilings) can be enforced and shown in the console.
USAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ai_usage.json")


# --------------------------------------------------------------------------- #
# Usage ledger
# --------------------------------------------------------------------------- #
def _month_key(d: date | None = None) -> str:
    d = d or date.today()
    return f"{d.year:04d}-{d.month:02d}"


def _load_usage() -> dict:
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_usage(u: dict):
    os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
    tmp = USAGE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(u, f, indent=2)
    os.replace(tmp, USAGE_PATH)


def usage_summary() -> dict:
    """For the console: this month's global + per-client call counts."""
    u = _load_usage()
    mk = _month_key()
    return u.get(mk, {})


def _bump_usage(client_id: str | None):
    u = _load_usage()
    mk = _month_key()
    month = u.setdefault(mk, {})
    month["__global__"] = month.get("__global__", 0) + 1
    if client_id:
        month[client_id] = month.get(client_id, 0) + 1
    _save_usage(u)


def _counts(client_id: str | None) -> tuple[int, int]:
    """(global_calls_this_month, this_client_calls_this_month)"""
    month = usage_summary()
    return month.get("__global__", 0), (month.get(client_id, 0) if client_id else 0)


# --------------------------------------------------------------------------- #
# Deterministic fallback — always available, no API needed
# --------------------------------------------------------------------------- #
def deterministic_summary(report) -> str:
    o = report.overall
    bits = [f"Spend ${o.spend:,.0f} returned {o.roas:.2f}× ROAS "
            f"(${o.conv_value:,.0f} in value) across {len(report.by_campaign)} campaigns."]
    best = report.best_roas_campaign
    worst = report.worst_roas_campaign
    if best:
        bits.append(f"Top performer: {best[0]} at {best[1].roas:.2f}× on "
                    f"${best[1].spend:,.0f} spend.")
    if worst and (not best or worst[0] != best[0]):
        bits.append(f"Weakest: {worst[0]} at {worst[1].roas:.2f}× — worth review.")
    bits.append(f"Overall CTR {o.ctr:.2f}%, CPA ${o.cpa:,.2f}, {o.conversions:,} conversions.")
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# Generic-profile insight (for any-data reports) — mirrors the Meta path's gates
# --------------------------------------------------------------------------- #
def deterministic_generic_summary(profile, plan: dict | None = None) -> str:
    """Number-based summary for an arbitrary dataset, no AI needed."""
    bits = [f"Dataset of {profile.n_rows:,} records across {profile.n_cols} fields."]
    # headline numeric(s)
    for name in (profile.numeric_columns or [])[:2]:
        c = profile.col(name)
        bits.append(f"{name}: total {c.total:,.0f}, average {c.mean:,.1f} "
                    f"(range {c.minimum:,.0f}–{c.maximum:,.0f}).")
    # leading category
    cats = (plan or {}).get("categories") or profile.category_columns
    if cats:
        c = profile.col(cats[0])
        if c and c.top_values:
            top_label, top_count = c.top_values[0]
            share = top_count / max(c.count, 1) * 100
            bits.append(f"Most common {cats[0]}: '{top_label}' "
                        f"({share:.0f}% of records).")
    if profile.date_column:
        dc = profile.col(profile.date_column)
        bits.append(f"Spans {dc.date_min} to {dc.date_max}.")
    return " ".join(bits)


def _generic_payload(profile, plan: dict | None = None) -> dict:
    plan = plan or {}
    cols = []
    for c in profile.columns:
        entry = {"name": c.name, "kind": c.kind}
        if c.kind == "number":
            entry.update(total=round(c.total or 0, 2), mean=round(c.mean or 0, 2),
                         min=c.minimum, max=c.maximum)
        elif c.kind == "category":
            entry.update(distinct=c.distinct, top=c.top_values[:5])
        elif c.kind == "date":
            entry.update(start=c.date_min, end=c.date_max)
        cols.append(entry)
    return {"n_rows": profile.n_rows, "n_cols": profile.n_cols,
            "primary_measure": plan.get("primary_num"),
            "key_categories": plan.get("categories", []),
            "date_column": profile.date_column, "columns": cols}


def generate_generic_insight(profile, plan: dict | None = None, *,
                             ai_opted_in: bool = False,
                             client_id: str | None = None,
                             client_ceiling: int | None = None,
                             report_type: str = "", client=None) -> str:
    """AI narrative for arbitrary data if every gate passes, else deterministic.
    Never raises."""
    if not ai_opted_in:
        return deterministic_generic_summary(profile, plan)

    global_calls, client_calls = _counts(client_id)
    if global_calls >= GLOBAL_MONTHLY_CALL_CEILING:
        return deterministic_generic_summary(profile, plan)
    if client_ceiling and client_calls >= client_ceiling:
        return deterministic_generic_summary(profile, plan)

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key and client is None:
        return deterministic_generic_summary(profile, plan)

    try:
        import anthropic
        c = client or anthropic.Anthropic(api_key=api_key)
        payload = _generic_payload(profile, plan)
        kind_hint = (f"The customer says this is '{report_type}' data. "
                     if report_type and report_type not in ("auto", "generic") else "")
        msg = c.messages.create(
            model=MODEL,
            max_tokens=300,
            system=("You are a data analyst. Given a profile of a dataset (column "
                    "types and stats) as JSON, write a tight 3-4 sentence read of "
                    "what the data shows: the scale, the standout numbers, the main "
                    "breakdown, any trend. " + kind_hint + "Plain, confident, no "
                    "preamble, no bullet points. Do not invent fields not present."),
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if not text:
            return deterministic_generic_summary(profile, plan)
        from engine.verify import verify_text
        v = verify_text(text, payload)
        if not v.ok:
            log.warning("generic insight discarded (%s) — deterministic "
                        "summary shipped", v.why())
            return deterministic_generic_summary(profile, plan)
        _bump_usage(client_id)
        return text
    except Exception as e:
        log.warning("generic AI insight failed (%s) — deterministic fallback", e)
        return deterministic_generic_summary(profile, plan)


# --------------------------------------------------------------------------- #
# AI insight with all the gates
# --------------------------------------------------------------------------- #
def _metrics_payload(report) -> dict:
    o = report.overall
    top = sorted(report.by_campaign.items(), key=lambda kv: kv[1].spend, reverse=True)[:8]
    return {
        "date_range": list(report.date_range),
        "overall": {"spend": round(o.spend, 2), "roas": round(o.roas, 2),
                    "ctr_pct": round(o.ctr, 2), "cpa": round(o.cpa, 2),
                    "conversions": o.conversions, "conv_value": round(o.conv_value, 2)},
        "campaigns": [{"name": n, "spend": round(m.spend, 2), "roas": round(m.roas, 2),
                       "ctr_pct": round(m.ctr, 2), "cpa": round(m.cpa, 2)} for n, m in top],
    }


def generate_insight(report, *, ai_opted_in: bool = False,
                     client_id: str | None = None,
                     client_ceiling: int | None = None,
                     client=None) -> str:
    """
    Return the report's insight paragraph. AI if every gate passes, else the
    deterministic summary. NEVER raises — the report must always get a summary.
    """
    # gate 1: opted in?
    if not ai_opted_in:
        return deterministic_summary(report)

    # gate 2 & 3: ceilings
    global_calls, client_calls = _counts(client_id)
    if global_calls >= GLOBAL_MONTHLY_CALL_CEILING:
        log.warning("global AI ceiling reached (%d) — deterministic fallback",
                    GLOBAL_MONTHLY_CALL_CEILING)
        return deterministic_summary(report)
    if client_ceiling and client_calls >= client_ceiling:
        log.info("client '%s' hit self-set ceiling (%d) — deterministic fallback",
                 client_id, client_ceiling)
        return deterministic_summary(report)

    # gate 4: key present + call succeeds
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key and client is None:
        return deterministic_summary(report)

    try:
        import anthropic
        c = client or anthropic.Anthropic(api_key=api_key)
        payload = _metrics_payload(report)
        msg = c.messages.create(
            model=MODEL,
            max_tokens=300,
            system=("You are a paid-media analyst. Given Meta Ads metrics as JSON, "
                    "write a tight 3-4 sentence performance read for a client: what "
                    "happened, what's working, what to watch. Plain, confident, no "
                    "preamble, no bullet points, no restating every number."),
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if not text:
            return deterministic_summary(report)
        from engine.verify import verify_text
        v = verify_text(text, payload)
        if not v.ok:
            log.warning("insight discarded (%s) — deterministic summary shipped", v.why())
            return deterministic_summary(report)
        _bump_usage(client_id)
        return text
    except Exception as e:
        log.warning("AI insight failed (%s) — deterministic fallback", e)
        return deterministic_summary(report)


# --------------------------------------------------------------------------- #
# Financial insight — quarterly fundamentals read in market context
# --------------------------------------------------------------------------- #
def deterministic_financial_summary(payload: dict) -> str:
    """Number-based read of the fundamentals payload, no AI needed."""
    bits = []
    for co in payload.get("companies", []):
        L = co.get("latest", {})
        rev = L.get("revenue")
        rev_s = (f"${rev/1e9:,.2f}B" if rev and abs(rev) >= 1e9
                 else f"${rev/1e6:,.1f}M" if rev else "—")
        piece = f"{co['ticker']}: {L.get('period','')} {L.get('fiscal_date','')} revenue {rev_s}"
        if co.get("revenue_yoy_pct") is not None:
            piece += f" ({co['revenue_yoy_pct']:+.1f}% YoY)"
        if L.get("net_margin_pct") is not None:
            piece += f", net margin {L['net_margin_pct']:.1f}%"
        if L.get("eps") is not None:
            piece += f", EPS {L['eps']:.2f}"
            if co.get("eps_yoy_pct") is not None:
                piece += f" ({co['eps_yoy_pct']:+.1f}% YoY)"
        bits.append(piece + ".")
    return " ".join(bits) or "No reportable fundamentals."


_FINANCIAL_SYSTEM = (
    "You are an equity analyst writing for retail investors. Given quarterly "
    "fundamentals for one or more companies as JSON (and, when present, a "
    "market_context describing the current environment), write a 4-6 sentence "
    "plain-English read per the fixed structure: what the latest quarter "
    "showed, the revenue and margin trajectory across the quarters on file, "
    "how that sits against the market context if one is provided, and what to "
    "watch next quarter. Plain, confident prose. No preamble, no bullet "
    "points, no advice to buy or sell. CRITICAL: use ONLY numbers that appear "
    "in the JSON — never estimate, extrapolate, or introduce outside figures; "
    "every number you write will be machine-checked against the source data."
)


def generate_financial_insight(payload: dict, *, ai_opted_in: bool = False,
                               client_id: str | None = None,
                               client_ceiling: int | None = None,
                               client=None) -> str:
    """AI narrative for a fundamentals payload if every gate passes AND the
    output survives verification; else the deterministic summary. Never raises.
    Gate 5 (verification) is what makes this lane deliverable to financial
    clients: no unverified number ever ships."""
    if not ai_opted_in:
        return deterministic_financial_summary(payload)

    global_calls, client_calls = _counts(client_id)
    if global_calls >= GLOBAL_MONTHLY_CALL_CEILING:
        return deterministic_financial_summary(payload)
    if client_ceiling and client_calls >= client_ceiling:
        return deterministic_financial_summary(payload)

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key and client is None:
        return deterministic_financial_summary(payload)

    try:
        import anthropic
        c = client or anthropic.Anthropic(api_key=api_key)
        msg = c.messages.create(
            model=MODEL,
            max_tokens=450,
            system=_FINANCIAL_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if not text:
            return deterministic_financial_summary(payload)
        # gate 5: verification — every number in the text must trace back to
        # the payload, or the AI text is discarded for the deterministic one.
        from engine.verify import verify_text
        # STRICT: the financial payload carries its own margins and growth
        # percentages, so every % claim must exist there verbatim.
        v = verify_text(text, payload, allow_shares=False)
        if not v.ok:
            # one corrective pass: hand the model its own rejected numbers
            log.info("verification failed (%s) — one corrective retry", v.why())
            bad = ", ".join(raw for raw, _ in v.unmatched)
            retry = c.messages.create(
                model=MODEL, max_tokens=450, system=_FINANCIAL_SYSTEM,
                messages=[
                    {"role": "user", "content": json.dumps(payload)},
                    {"role": "assistant", "content": text},
                    {"role": "user", "content":
                        f"Verification rejected these numbers as not present in "
                        f"the JSON: {bad}. Rewrite your analysis using ONLY "
                        f"numbers that appear verbatim as JSON values. Do not "
                        f"compute or derive anything."},
                ],
            )
            text = "".join(b.text for b in retry.content
                           if getattr(b, "type", "") == "text").strip()
            v = verify_text(text, payload, allow_shares=False)
            if not text or not v.ok:
                log.warning("financial insight discarded after retry (%s) — "
                            "deterministic summary shipped instead", v.why())
                return deterministic_financial_summary(payload)
            _bump_usage(client_id)   # count the retry
        _bump_usage(client_id)
        return text
    except Exception as e:
        log.warning("financial AI insight failed (%s) — deterministic fallback", e)
        return deterministic_financial_summary(payload)
