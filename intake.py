"""
intake.py — Front door. Turns a filled-out client form into a provisioned,
keyed client, then routes their dropped data files to reports.

Two stages, both anchored on client_id:

  ONBOARDING (form -> registry + workspace)
    A client fills out an intake form (JSON) with: client_id, brand, api_key,
    schedule. They drop it in intake/onboarding/. process_onboarding():
      1. reads the form
      2. encrypts the key into the registry (encrypted at rest)
      3. SHREDS the plaintext key from the form file immediately
      4. provisions the client's workspace: clients/<id>/{inbox,output,archive}
      5. archives the (now key-less) form
    The plaintext key's life on disk is the few milliseconds between read and
    shred. After onboarding it exists only encrypted, only in the store.

  DATA INTAKE (dropped CSV -> report)
    The client drops their records CSV into clients/<id>/inbox/. process_inbox()
    matches it to the client by the FOLDER (client_id is the anchor), runs the
    keyed pipeline, writes the report to clients/<id>/output/, and moves the CSV
    to clients/<id>/archive/.

Lane note: this whole module is the KEYED lane's front door. The basic no-key
walk-in lane (runner.run_csv) is untouched and unreachable from here.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import json
import shutil
import logging
from datetime import datetime, timezone

from registry import Registry, RegistryError

log = logging.getLogger("intake")

ROOT = os.path.dirname(__file__)
ONBOARD_DIR = os.path.join(ROOT, "intake", "onboarding")
ONBOARD_ARCHIVE = os.path.join(ROOT, "intake", "onboarding", "_archived")
CLIENTS_ROOT = os.path.join(ROOT, "clients")

# The fields an intake form must carry. api_key is required for a keyed client.
FORM_FIELDS = ("client_id", "brand", "api_key", "schedule")

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class IntakeError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Workspace provisioning
# --------------------------------------------------------------------------- #
def client_dir(client_id: str) -> str:
    return os.path.join(CLIENTS_ROOT, _safe(client_id))


def _safe(s: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s).strip("_")
    if not out:
        raise IntakeError(f"unusable client_id: {s!r}")
    return out


def provision_workspace(client_id: str) -> dict:
    """Create clients/<id>/{inbox,output,archive}. Idempotent."""
    base = client_dir(client_id)
    paths = {sub: os.path.join(base, sub) for sub in ("inbox", "output", "archive")}
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    log.info("workspace ready for '%s' -> %s", client_id, base)
    return paths


# --------------------------------------------------------------------------- #
# Key shredding
# --------------------------------------------------------------------------- #
def _shred_key_in_form(form_path: str, form: dict):
    """
    Overwrite the api_key in the on-disk form with a destroyed marker and rewrite
    the file, so the plaintext key does not linger in the onboarding folder.
    The real secret is already encrypted in the registry by the time this runs.
    """
    form = dict(form)
    if form.get("api_key"):
        form["api_key"] = "__SHREDDED_AFTER_INTAKE__"
    with open(form_path, "w", encoding="utf-8") as f:
        json.dump(form, f, indent=2)
    # also drop our in-memory copy's plaintext
    return None


def _parse_schedule(sched) -> list[int]:
    if not sched:
        return []
    if isinstance(sched, list):
        toks = sched
    else:
        toks = str(sched).split(",")
    out = []
    for t in toks:
        t = str(t).strip().lower()[:3]
        if t in _DAYS:
            out.append(_DAYS[t])
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# Onboarding
# --------------------------------------------------------------------------- #
def process_onboarding(registry: Registry | None = None) -> list[str]:
    """
    Scan intake/onboarding/ for *.json forms, onboard each one.
    Returns list of client_ids successfully onboarded.
    """
    os.makedirs(ONBOARD_DIR, exist_ok=True)
    os.makedirs(ONBOARD_ARCHIVE, exist_ok=True)
    registry = registry or Registry()
    done = []

    for name in sorted(os.listdir(ONBOARD_DIR)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        form_path = os.path.join(ONBOARD_DIR, name)
        try:
            with open(form_path, encoding="utf-8") as f:
                form = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.error("skipping unreadable form %s: %s", name, e)
            continue

        missing = [k for k in FORM_FIELDS if k not in form]
        if missing:
            log.error("form %s missing fields %s — skipping", name, missing)
            continue

        cid = form["client_id"]
        api_key = form.get("api_key") or ""
        if not api_key or api_key == "__SHREDDED_AFTER_INTAKE__":
            log.warning("form %s has no live key (already processed?) — skipping", name)
            continue

        try:
            paths = provision_workspace(cid)
            # if the client supplied a logo path in the form, copy it into their
            # workspace so it's always available at render time
            logo_file = ""
            src_logo = form.get("logo_path") or form.get("logo") or ""
            if src_logo and os.path.exists(src_logo):
                ext = os.path.splitext(src_logo)[1].lower() or ".png"
                logo_file = f"logo{ext}"
                shutil.copy(src_logo, os.path.join(client_dir(cid), logo_file))

            registry.add_client(
                client_id=cid,
                brand=form["brand"],
                api_key=api_key,
                schedule_days=_parse_schedule(form.get("schedule")),
                report_config=form.get("report_config", {}),
                business_name=form.get("business_name", ""),
                email=form.get("email", ""),
                phone=form.get("phone", ""),
                logo_file=logo_file,
                ai_insight=bool(form.get("ai_insight", False)),
                ai_ceiling=form.get("ai_ceiling"),
                report_type=form.get("report_type", "auto") or "auto",
            )
        except RegistryError as e:
            log.error("could not onboard '%s': %s", cid, e)
            continue

        # key is now encrypted in the store -> destroy the plaintext at the source
        _shred_key_in_form(form_path, form)
        api_key = None
        form["api_key"] = None

        # archive the (now key-less) form
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        shutil.move(form_path, os.path.join(ONBOARD_ARCHIVE, f"{stamp}_{name}"))

        log.info("onboarded '%s' (%s); key encrypted, plaintext shredded", cid, form["brand"])
        done.append(cid)

    return done


# --------------------------------------------------------------------------- #
# Data intake (dropped records -> report)
# --------------------------------------------------------------------------- #
def process_inbox(registry: Registry | None = None, fetch_fn=None) -> list:
    """
    For each client, pick up CSVs dropped in clients/<id>/inbox/, build their
    report into clients/<id>/output/, archive the CSV. The client_id is taken
    from the FOLDER the file sits in — that's the anchor.

    This uses the dropped CSV directly as the client's data (no live pull needed
    when they bring their own records), so it works even before the Meta
    connector is bolted on.
    """
    from engine.report import Branding
    from engine.router import render as route_render, decide
    from engine.profile import profile_data, load_csv_rows
    from engine.generic_report import summarize
    from engine.insight import generate_insight, generate_generic_insight

    registry = registry or Registry()
    results = []

    if not os.path.isdir(CLIENTS_ROOT):
        return results

    for entry in sorted(os.listdir(CLIENTS_ROOT)):
        base = os.path.join(CLIENTS_ROOT, entry)
        inbox = os.path.join(base, "inbox")
        if not os.path.isdir(inbox):
            continue

        # match folder -> client record (the client_id anchor)
        try:
            client = registry.get(entry)
        except RegistryError:
            log.warning("inbox folder '%s' has no client record — skipping", entry)
            continue
        if not client.active:
            log.info("client '%s' inactive — skipping inbox", entry)
            continue

        out_dir = os.path.join(base, "output")
        arch_dir = os.path.join(base, "archive")
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(arch_dir, exist_ok=True)

        logo_path = os.path.join(base, client.logo_file) if client.logo_file else ""
        branding = Branding(
            brand=client.brand, business_name=client.business_name,
            email=client.email, phone=client.phone, logo_path=logo_path,
        )

        for fn in sorted(os.listdir(inbox)):
            if not fn.lower().endswith(".csv"):
                continue
            csv_path = os.path.join(inbox, fn)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            try:
                rows = load_csv_rows(csv_path)
                if not rows:
                    raise ValueError("empty CSV (no data rows)")
                headers = list(rows[0].keys())
                rendered_as, note = decide(client.report_type, headers)

                # build the right insight for whichever way it routed
                if rendered_as == "meta":
                    from engine.analytics import load_report
                    rpt = load_report(csv_path)
                    insight_text = generate_insight(
                        rpt, ai_opted_in=client.ai_insight,
                        client_id=entry, client_ceiling=client.ai_ceiling)
                else:
                    prof = profile_data(rows)
                    plan = summarize(rows, prof)
                    insight_text = generate_generic_insight(
                        prof, plan, ai_opted_in=client.ai_insight,
                        client_id=entry, client_ceiling=client.ai_ceiling,
                        report_type=client.report_type)

                routed = route_render(
                    csv_path, out_dir, report_type=client.report_type,
                    brand=client.brand, branding=branding,
                    insight_text=insight_text, file_stem=f"report_{stamp}",
                    make_pdf=True)

                registry.mark_run(entry)
                shutil.move(csv_path, os.path.join(arch_dir, f"{stamp}_{fn}"))

                # OFF-RAMP: copy to outbox (always) + email if the seam is active
                from engine.delivery import deliver
                dresult = deliver(routed.html_path, entry, brand=client.brand,
                                  to_email=client.email, pdf_path=routed.pdf_path)
                log.info("[%s] %s report -> %s (%s) | delivered: outbox=%s emailed=%s",
                         entry, routed.rendered_as, routed.html_path, note,
                         os.path.basename(dresult["outbox"]), dresult["emailed"])
                results.append((entry, True, routed.html_path))
            except Exception as e:
                log.error("[%s] could not process %s: %s", entry, fn, e)
                bad = os.path.join(arch_dir, f"{stamp}_REJECTED_{fn}")
                try:
                    shutil.move(csv_path, bad)
                except OSError:
                    pass
                results.append((entry, False, str(e)))

    return results


def make_blank_form(client_id: str, brand: str = "", path: str | None = None) -> str:
    """Write a blank intake form a client can fill in (helper for the form flow)."""
    os.makedirs(ONBOARD_DIR, exist_ok=True)
    form = {
        "client_id": client_id,
        "brand": brand,
        "api_key": "",          # client pastes their key here; shredded after intake
        "schedule": "mon,wed,fri",
        "business_name": "",     # optional — all four below can be left blank
        "email": "",
        "phone": "",
        "logo_path": "",         # optional path to a png/jpg logo
        "ai_insight": False,      # opt in to AI-written analysis on the report
        "ai_ceiling": None,       # optional self-set monthly AI-report ceiling
        "report_type": "auto",    # what is this data? auto|generic|meta|sales|catalog|survey
        "report_config": {},
    }
    path = path or os.path.join(ONBOARD_DIR, f"{_safe(client_id)}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(form, f, indent=2)
    return path
