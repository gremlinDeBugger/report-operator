"""
console.py — Operator command line. Your whole book at a glance.

Usage:
    python console.py keygen                 # make a master key (set in env, never commit)
    python console.py add  <id> <brand> --key <APIKEY> --days mon,wed,fri
    python console.py list                    # who's configured, last run, status
    python console.py run  <id>               # run one keyed client now (manual)
    python console.py due                     # show who's due today (dry run)
    python console.py tick                    # run everything due now (one pass)
    python console.py revoke <id>             # deactivate + wipe stored secret
    python console.py report <csv> <brand>    # BASIC lane: one-off CSV report, no key

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import sys
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_RDAYS = {v: k for k, v in _DAYS.items()}


def _parse_days(s: str) -> list[int]:
    if not s:
        return []
    out = []
    for tok in s.lower().split(","):
        tok = tok.strip()
        if tok not in _DAYS:
            raise SystemExit(f"bad day '{tok}' — use mon,tue,wed,thu,fri,sat,sun")
        out.append(_DAYS[tok])
    return sorted(set(out))


def _fmt_days(days: list[int]) -> str:
    return ",".join(_RDAYS[d] for d in sorted(days)) if days else "(unscheduled)"


def cmd_keygen(_):
    import registry
    print(registry.generate_master_key())
    print("# set this in your environment, e.g.:", file=sys.stderr)
    print("#   export OPERATOR_MASTER_KEY='...'   (NEVER commit it)", file=sys.stderr)


def cmd_add(args):
    from registry import Registry
    r = Registry()
    c = r.add_client(args.id, args.brand, args.key,
                     schedule_days=_parse_days(args.days or ""),
                     ai_insight=args.ai, report_type=args.type)
    print(f"added '{c.client_id}' ({c.brand}) — schedule {_fmt_days(c.schedule_days)}, "
          f"type={c.report_type}, ai={'on' if c.ai_insight else 'off'}")


def cmd_list(args):
    from registry import Registry
    r = Registry()
    clients = r.list_clients()
    if not clients:
        print("no clients configured.")
        return
    print(f"{'ID':<16}{'BRAND':<22}{'SCHEDULE':<18}{'STATUS':<10}LAST RUN")
    print("-" * 84)
    for c in clients:
        status = "active" if c.active else "revoked"
        print(f"{c.client_id:<16}{c.brand[:21]:<22}{_fmt_days(c.schedule_days):<18}"
              f"{status:<10}{c.last_run or '—'}")


def cmd_run(args):
    from registry import Registry
    from runner import run_client
    r = Registry()
    res = run_client(r, args.id, fetch_fn=_demo_fetch if args.demo else None)
    if res.ok:
        print(f"✓ {args.id}: {res.html_path}" + (f" | {res.pdf_path}" if res.pdf_path else ""))
    else:
        print(f"✗ {args.id}: {res.error}")


def cmd_due(args):
    from registry import Registry
    from scheduler import due_clients
    r = Registry()
    due = due_clients(r)
    if not due:
        print("nothing due today.")
        return
    print("due today:")
    for c in due:
        print(f"  {c.client_id} ({c.brand})")


def cmd_tick(args):
    from registry import Registry
    from scheduler import run_due
    r = Registry()
    results = run_due(r, fetch_fn=_demo_fetch if args.demo else None)
    print(f"ran {len(results)} job(s); {sum(1 for x in results if x.ok)} ok, "
          f"{sum(1 for x in results if not x.ok)} failed.")


def cmd_revoke(args):
    from registry import Registry
    r = Registry()
    r.revoke(args.id)
    print(f"revoked '{args.id}' (secret wiped).")


def cmd_report(args):
    """BASIC lane — no registry touched at all."""
    from runner import run_csv
    res = run_csv(args.csv, brand=args.brand, out_dir=args.out,
                  ai_insight=args.ai, report_type=args.type)
    if res.ok:
        print(f"✓ {res.html_path}" + (f" | {res.pdf_path}" if res.pdf_path else ""))
    else:
        print(f"✗ {res.error}")


def _demo_fetch(api_key, report_config, dest_csv):
    """Stand-in for the live Meta pull, for local testing. Copies the bundled
    sample export so `run`/`tick --demo` produce a real report end to end."""
    import shutil, os
    sample = os.path.join(os.path.dirname(__file__), "engine", "sample_fb_export.csv")
    if not os.path.exists(sample):
        sample = report_config.get("sample_csv", "")
    shutil.copy(sample, dest_csv)


def build_parser():
    p = argparse.ArgumentParser(description="Report Operator console")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen").set_defaults(func=cmd_keygen)

    a = sub.add_parser("add"); a.add_argument("id"); a.add_argument("brand")
    a.add_argument("--key", required=True); a.add_argument("--days", default="")
    a.add_argument("--ai", action="store_true", help="enable AI insight for this client")
    a.add_argument("--type", default="auto",
                   help="data type: auto|generic|meta|sales|catalog|survey")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list").set_defaults(func=cmd_list)

    rp = sub.add_parser("run"); rp.add_argument("id")
    rp.add_argument("--demo", action="store_true")
    rp.set_defaults(func=cmd_run)

    sub.add_parser("due").set_defaults(func=cmd_due)

    tp = sub.add_parser("tick"); tp.add_argument("--demo", action="store_true")
    tp.set_defaults(func=cmd_tick)

    rv = sub.add_parser("revoke"); rv.add_argument("id"); rv.set_defaults(func=cmd_revoke)

    rep = sub.add_parser("report"); rep.add_argument("csv"); rep.add_argument("brand")
    rep.add_argument("--out", default="output")
    rep.add_argument("--ai", action="store_true")
    rep.add_argument("--type", default="auto",
                     help="data type: auto|generic|meta|sales|catalog|survey")
    rep.set_defaults(func=cmd_report)

    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)
