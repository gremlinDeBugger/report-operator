"""
delivery.py — The off-ramp. Gets a finished report back to the customer.

Two mechanisms, matching how delivery actually happens at different stages:

  OUTBOX (works today, no setup)
    Every finished report is copied into a single tidy outbox folder with a
    clear, professional name: <client>_<YYYY-MM-DD>_report.pdf. You hand these
    off however you like — attach to an email yourself, drop in a shared folder,
    hand over on a stick. No external dependency; works right now.

  EMAIL (activatable seam)
    Auto-email the finished report to the customer as an attachment. This needs
    email credentials (SMTP settings in the environment) — until those are set,
    it's a built-but-dormant seam, exactly like the API-key and vision seams.
    When the creds are present, delivery becomes hands-off.

Delivery never blocks report generation: if an email fails or creds are missing,
the report is still safely in the outbox. The off-ramp degrades gracefully.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import shutil
import logging
import smtplib
from email.message import EmailMessage
from datetime import date

log = logging.getLogger("delivery")

# where the tidy, hand-off-ready copies collect
DEFAULT_OUTBOX = os.path.join(os.path.dirname(os.path.dirname(__file__)), "outbox")

# email seam reads these from the environment; absent -> email stays dormant
SMTP_HOST_ENV = "SMTP_HOST"
SMTP_PORT_ENV = "SMTP_PORT"
SMTP_USER_ENV = "SMTP_USER"
SMTP_PASS_ENV = "SMTP_PASS"
SMTP_FROM_ENV = "SMTP_FROM"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s).strip("_") or "client"


# --------------------------------------------------------------------------- #
# Outbox — always available
# --------------------------------------------------------------------------- #
def to_outbox(report_path: str, client_id: str, brand: str = "",
              outbox: str = DEFAULT_OUTBOX) -> str:
    """
    Copy a finished report into the outbox with a clean, dated, professional
    name. Returns the outbox path. This is the delivery that always works.
    """
    os.makedirs(outbox, exist_ok=True)
    ext = os.path.splitext(report_path)[1] or ".pdf"
    stamp = date.today().isoformat()
    label = _safe(brand or client_id)
    dest = os.path.join(outbox, f"{label}_{stamp}_report{ext}")
    # avoid clobbering if multiple runs same day
    n = 2
    base_dest = dest
    while os.path.exists(dest):
        root, e = os.path.splitext(base_dest)
        dest = f"{root}_{n}{e}"
        n += 1
    shutil.copy(report_path, dest)
    log.info("delivered to outbox: %s", dest)
    return dest


# --------------------------------------------------------------------------- #
# Email — activatable seam
# --------------------------------------------------------------------------- #
def email_configured() -> bool:
    return all(os.environ.get(v) for v in (SMTP_HOST_ENV, SMTP_USER_ENV,
                                           SMTP_PASS_ENV, SMTP_FROM_ENV))


def send_email(to_addr: str, subject: str, body: str,
               attachments: list[str] | None = None) -> bool:
    """
    Send a report by email. Returns True on success, False if not configured or
    the send fails — NEVER raises, so a delivery problem can't crash the run.
    The report is always already in the outbox as a fallback.
    """
    if not email_configured():
        log.info("email seam dormant (SMTP creds not set) — outbox only")
        return False
    if not to_addr:
        log.info("no customer email on record — outbox only")
        return False
    try:
        msg = EmailMessage()
        msg["From"] = os.environ[SMTP_FROM_ENV]
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        for path in attachments or []:
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            sub = "pdf" if path.lower().endswith(".pdf") else "octet-stream"
            msg.add_attachment(data, maintype="application", subtype=sub,
                               filename=os.path.basename(path))
        host = os.environ[SMTP_HOST_ENV]
        port = int(os.environ.get(SMTP_PORT_ENV, "587"))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(os.environ[SMTP_USER_ENV], os.environ[SMTP_PASS_ENV])
            s.send_message(msg)
        log.info("emailed report to %s", to_addr)
        return True
    except Exception as e:
        log.warning("email send failed (%s) — report is still in the outbox", e)
        return False


# --------------------------------------------------------------------------- #
# The combined off-ramp
# --------------------------------------------------------------------------- #
def deliver(report_path: str, client_id: str, brand: str = "",
            to_email: str = "", outbox: str = DEFAULT_OUTBOX,
            pdf_path: str | None = None) -> dict:
    """
    Deliver a finished report: always to the outbox, and by email too if the
    seam is active and the customer has an address on record. Returns a summary
    of what happened. Prefers attaching the PDF if available, else the HTML.
    """
    attach = pdf_path if (pdf_path and os.path.exists(pdf_path)) else report_path
    outbox_path = to_outbox(attach, client_id, brand, outbox)
    emailed = False
    if to_email:
        emailed = send_email(
            to_email,
            subject=f"{brand or client_id} — Report ({date.today().isoformat()})",
            body=(f"Hi,\n\nYour latest report is attached.\n\n"
                  f"— {brand or 'Reporting'}\n"),
            attachments=[attach],
        )
    return {"outbox": outbox_path, "emailed": emailed,
            "email_active": email_configured()}
