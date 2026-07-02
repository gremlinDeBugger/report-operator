"""
scheduler.py — Unattended, catch-up-aware report scheduler (keyed lane only).

Design guarantees, matching the two requirements that drove this build:

1. NON-INTERFERENCE WITH BASIC CUSTOMERS.
   The scheduler's entire universe is registry.active_clients(). A basic CSV
   customer has no registry record, so the scheduler literally cannot see them
   and cannot fire a report for them. There is no code path here that produces
   a report for anyone without a stored key. No record = no run.

2. AUTOMATED, WITH HONEST BEHAVIOUR ON A SLEEPY HOST.
   On an always-on host (mac mini / VPS) this just runs. On a laptop that
   sleeps, a scheduled day could pass while the machine is off. So the
   scheduler is CATCH-UP AWARE: on each wake/tick it asks "is this client due
   today and not yet run today?" and runs the ones that were missed, rather
   than silently skipping. Moving to an always-on host doesn't change the
   code — it just means runs are never missed in the first place.

This file decides WHO runs and WHEN. It delegates HOW to runner.run_client,
and never decrypts or touches keys itself.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import time
import logging
from datetime import datetime, date

from runner import run_client

log = logging.getLogger("scheduler")


def _today_weekday(today: date | None = None) -> int:
    return (today or date.today()).weekday()   # 0=Mon .. 6=Sun


def _already_ran_today(last_run: str | None, today: date) -> bool:
    if not last_run:
        return False
    try:
        return datetime.fromisoformat(last_run).date() >= today
    except ValueError:
        return False


def due_clients(registry, today: date | None = None):
    """
    The clients that SHOULD run as of `today`: active, scheduled for today's
    weekday, and not already run today. This is the catch-up check — if the
    host was asleep on the scheduled day and it's still that day's window, they
    show up here until they've actually run.
    """
    today = today or date.today()
    wd = _today_weekday(today)
    due = []
    for c in registry.active_clients():
        if wd in c.schedule_days and not _already_ran_today(c.last_run, today):
            due.append(c)
    return due


def run_due(registry, today: date | None = None, fetch_fn=None) -> list:
    """
    Run every due client, each isolated: one client's failure is logged and the
    loop continues to the next. Returns a list of RunResult.
    """
    today = today or date.today()
    due = due_clients(registry, today)
    if not due:
        log.info("nothing due today (%s)", today.isoformat())
        return []

    log.info("%d client(s) due today (%s)", len(due), today.isoformat())
    results = []
    for c in due:
        try:
            res = run_client(registry, c.client_id, fetch_fn=fetch_fn)
        except Exception as e:
            # A crash in one client must never stop the others. This is the
            # blast-radius containment: failures are per-client, never global.
            log.exception("unexpected failure running '%s': %s", c.client_id, e)
            continue
        if res.ok:
            log.info("  ✓ %s (%s)", c.client_id, c.brand)
        else:
            log.error("  ✗ %s (%s): %s", c.client_id, c.brand, res.error)
        results.append(res)
    return results


def serve_forever(registry, fetch_fn=None, tick_seconds: int = 1800,
                  drain_inboxes: bool = True):
    """
    Unattended loop. Each tick: (1) run anything due on schedule, including
    catch-up for missed days, then (2) drain every client's inbox so dropped
    CSVs become reports automatically. Sleeps, repeats. This is what runs as a
    launchd job (mac mini) or systemd service (VPS) — same code either way.

    drain_inboxes=True is what makes the system hands-off: a client drops a CSV
    in their input folder and a report appears on the next tick, no command run.
    """
    log.info("scheduler serving; tick every %ds (inbox draining: %s). Ctrl-C to stop.",
             tick_seconds, drain_inboxes)
    try:
        while True:
            run_due(registry, fetch_fn=fetch_fn)
            if drain_inboxes:
                try:
                    from intake import process_inbox
                    results = process_inbox(registry)
                    if results:
                        ok = sum(1 for _, s, _ in results if s)
                        log.info("inbox drain: %d processed (%d ok)", len(results), ok)
                except Exception as e:
                    # inbox draining must never crash the scheduler loop
                    log.exception("inbox drain error (continuing): %s", e)
            time.sleep(tick_seconds)
    except KeyboardInterrupt:
        log.info("scheduler stopped.")
