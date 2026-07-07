"""
service/app.py — HTTP front door for report-operator.

One job: take a request from the website form, run the existing pipeline
(fetch -> verify -> render -> deliver), and hand the finished report back.

Design rules, in order of importance:

  1. BYOK, zero retention. The customer's provider API key arrives in the
     request body over HTTPS, lives in a local variable for the duration of
     the fetch, and is never logged, never written to disk, never stored.
     There is no database here on purpose.

  2. This module ADDS a lane, it does not modify one. It imports the same
     public entry points the CLI uses (connectors.fundamentals, runner.run_csv,
     engine.delivery). If this folder were deleted, report-operator is
     untouched.

  3. Fail loud, clean up always. Every run happens in a throwaway temp dir
     that is removed before the response goes out, success or not.

Endpoints:
  GET  /api/health    -> {"ok": true}
  POST /api/generate  -> JSON: report as base64 PDF (HTML fallback) + status

Demo mode: provider="demo" runs the full pipeline on the bundled fixture
data — no key required. That is what powers the "try it without a key"
button on the site.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import re
import sys
import time
import base64
import shutil
import logging
import tempfile
import urllib.parse
from collections import defaultdict, deque

# service/ sits inside the repo; make the repo root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from connectors.fundamentals import (fetch_quarterly_fundamentals,
                                     fetch_quarterly_fundamentals_av,
                                     fixture_fetch_fn)
from runner import run_csv
from engine import delivery

# Upload guard rails for the walk-in CSV lane.
MAX_CSV_BYTES = int(os.environ.get("MAX_CSV_BYTES", str(5 * 1024 * 1024)))  # 5 MB
ALLOWED_REPORT_TYPES = {"auto", "generic", "meta", "sales", "catalog",
                        "survey", "financial"}

log = logging.getLogger("service")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="GremlinHunter Reporting", docs_url=None, redoc_url=None)

# The static site lives on a different origin (GitHub Pages). Lock this down
# to your Pages URL once it exists; "*" is acceptable only while testing.
ALLOWED_ORIGINS = [o.strip() for o in
                   os.environ.get("ALLOWED_ORIGINS", "*").split(",")]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["POST", "GET"], allow_headers=["*"])

PROVIDERS = {
    "fmp": fetch_quarterly_fundamentals,
    "alphavantage": fetch_quarterly_fundamentals_av,
    "demo": fixture_fetch_fn,
}

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --------------------------------------------------------------------------- #
# Rate limit — small, in-memory, per-IP. This is a demo front door, not a CDN.
# Restarting the service resets it; that is fine at this scale.
# --------------------------------------------------------------------------- #
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_HOUR", "6"))
_hits: dict[str, deque] = defaultdict(deque)

def _rate_ok(ip: str) -> bool:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return False
    q.append(now)
    return True


class GenerateRequest(BaseModel):
    provider: str
    tickers: list[str] = []
    api_key: str = ""
    email: str = ""
    brand: str = ""

    @field_validator("provider")
    @classmethod
    def _provider(cls, v):
        v = v.strip().lower()
        if v not in PROVIDERS:
            raise ValueError("provider must be one of: fmp, alphavantage, demo")
        return v

    @field_validator("tickers")
    @classmethod
    def _tickers(cls, v):
        cleaned = [t.strip().upper() for t in v if t and t.strip()]
        if len(cleaned) > 4:
            raise ValueError("4 tickers max per report")
        for t in cleaned:
            if not TICKER_RE.match(t):
                raise ValueError(f"'{t}' does not look like a ticker symbol")
        return cleaned

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = v.strip()
        if v and not EMAIL_RE.match(v):
            raise ValueError("that email address does not look valid")
        return v

    @field_validator("brand")
    @classmethod
    def _brand(cls, v):
        return v.strip()[:60]


@app.get("/api/health")
def health():
    return {"ok": True, "email_configured": delivery.email_configured()}


def _client_ip(request: Request) -> str:
    # Behind Render's proxy, request.client.host is the proxy's address for
    # every visitor, not the caller's — that would collapse the per-IP limit
    # into one global limit. Render sets X-Forwarded-For; take the first hop.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/generate")
def generate(req: GenerateRequest, request: Request):
    ip = _client_ip(request)
    if not _rate_ok(ip):
        raise HTTPException(429, "Rate limit reached — this demo allows "
                                 f"{RATE_LIMIT} reports per hour per address. "
                                 "Try again later.")

    if req.provider != "demo":
        if not req.api_key.strip():
            raise HTTPException(422, "An API key is required for live data. "
                                     "Use the sample-data run to try it "
                                     "without one.")
        if not req.tickers:
            raise HTTPException(422, "Enter at least one ticker symbol.")

    brand = req.brand or "Sample Client"
    fetch_fn = PROVIDERS[req.provider]
    work = tempfile.mkdtemp(prefix="run_")
    try:
        csv_path = os.path.join(work, "fundamentals.csv")
        try:
            # The key is passed straight through and goes out of scope with
            # this request. Do not add logging inside this block.
            # Demo mode ignores typed tickers — the fixture decides coverage.
            tickers = [] if req.provider == "demo" else req.tickers
            fetch_fn(req.api_key, {"tickers": tickers, "quarters": 8},
                     csv_path)
        except Exception as e:
            # Provider errors (bad key, rate limit, unknown ticker) surface
            # verbatim minus anything that could echo the key back. Connectors
            # embed the key both raw and URL-quoted (it's part of the request
            # URL), so strip both forms.
            msg = str(e)
            if req.api_key:
                msg = msg.replace(req.api_key, "***")
                msg = msg.replace(urllib.parse.quote(req.api_key), "***")
            raise HTTPException(502, f"Data provider error: {msg[:300]}")

        result = run_csv(csv_path, brand=brand, out_dir=work,
                         report_type="financial")
        if not result.ok:
            raise HTTPException(500, f"Report build failed: {result.error}")

        report_path = result.pdf_path or result.html_path
        emailed = False
        if req.email:
            emailed = delivery.send_email(
                req.email,
                subject=f"Quarterly fundamentals report — {brand}",
                body="Your report is attached.\n\n— GremlinHunter Reporting",
                attachments=[report_path],
            )

        with open(report_path, "rb") as f:
            payload = base64.b64encode(f.read()).decode("ascii")

        return {
            "ok": True,
            "filename": os.path.basename(report_path),
            "content_type": ("application/pdf" if report_path.endswith(".pdf")
                             else "text/html"),
            "report_b64": payload,
            "emailed": emailed,
            "email_configured": delivery.email_configured(),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CSV lane — the walk-in job. Customer uploads their own CSV, gets a report.
# This exposes runner.run_csv (the "basic lane") over the web. No API key, no
# provider fetch, no credential machinery — the file IS the data. Same temp-dir
# discipline, same rate limit, same email off-ramp as /api/generate.
# --------------------------------------------------------------------------- #
@app.post("/api/generate-csv")
async def generate_csv(request: Request,
                       file: UploadFile = File(...),
                       report_type: str = Form("auto"),
                       email: str = Form(""),
                       brand: str = Form("")):
    ip = _client_ip(request)
    if not _rate_ok(ip):
        raise HTTPException(429, "Rate limit reached — this demo allows "
                                 f"{RATE_LIMIT} reports per hour per address. "
                                 "Try again later.")

    # Validate inputs before touching disk.
    name = (file.filename or "").strip()
    if not name.lower().endswith(".csv"):
        raise HTTPException(422, "Please upload a .csv file.")
    rtype = report_type.strip().lower() or "auto"
    if rtype not in ALLOWED_REPORT_TYPES:
        raise HTTPException(422, "report_type must be one of: "
                                 + ", ".join(sorted(ALLOWED_REPORT_TYPES)))
    email = email.strip()
    if email and not EMAIL_RE.match(email):
        raise HTTPException(422, "that email address does not look valid")
    brand = (brand.strip() or "Sample Client")[:60]

    raw = await file.read()
    if not raw:
        raise HTTPException(422, "That file is empty.")
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, f"File too large — {MAX_CSV_BYTES // (1024*1024)} MB max.")

    work = tempfile.mkdtemp(prefix="csv_")
    try:
        csv_path = os.path.join(work, "upload.csv")
        with open(csv_path, "wb") as f:
            f.write(raw)

        result = run_csv(csv_path, brand=brand, out_dir=work,
                         report_type=rtype)
        if not result.ok:
            # run_csv returns its error string rather than raising; surface it.
            raise HTTPException(422, f"Couldn't build a report from that CSV: "
                                     f"{result.error}")

        report_path = result.pdf_path or result.html_path
        emailed = False
        if email:
            emailed = delivery.send_email(
                email,
                subject=f"Your report — {brand}",
                body="Your report is attached.\n\n— GremlinHunter Reporting",
                attachments=[report_path],
            )

        with open(report_path, "rb") as f:
            payload = base64.b64encode(f.read()).decode("ascii")

        return {
            "ok": True,
            "filename": os.path.basename(report_path),
            "content_type": ("application/pdf" if report_path.endswith(".pdf")
                             else "text/html"),
            "report_b64": payload,
            "emailed": emailed,
            "email_configured": delivery.email_configured(),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
