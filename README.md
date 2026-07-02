# Report Operator

A multi-client operations layer around a flexible reporting engine. It onboards
clients, holds their API keys **encrypted at rest**, and generates **their-branded**
reports — on a schedule or on demand — from **any tabular data**, with each client
cleanly isolated from every other.

The customer declares what their data is at intake; the system routes it to the
right report. Ad data renders through a sharp Meta Ads template; anything else
(sales, survey, sensor, catalog, arbitrary CSVs) renders through a generic engine
that profiles the data and builds a professional report from whatever it finds.

Built to run unchanged on a laptop, then a Mac mini, then a VPS. Moving hosts is
a file copy, not a migration.

> Built by **Jared Jowett (GremlinHunter)** — Python automation & systems.
> Grew out of [fb-ads-reporter], a single-purpose Meta Ads report tool,
> generalized into a multi-tenant any-data reporting platform.

---

## How report routing works

The customer declares a `report_type` at signup. It is **authoritative** — the
system honors it rather than guessing:

```
report_type        renders as
-----------        ----------
"meta"             sharp Meta Ads template (if ad columns confirmed; else generic)
"auto"             sniffs the data: Meta ad data -> Meta template, else -> generic
"generic"          generic profiler-driven report
"sales"/"catalog"/ generic report today; auto-upgrades to a specialized template
"survey"           the day one is built — no rebuild, just declare it
```

The generic engine **profiles any CSV** (works out which columns are numbers,
dates, categories, or text), then builds KPIs, a trend chart, and a category
breakdown from whatever it finds. Data too thin to report on (a single row, or
nothing but free text) is rejected with a clear reason rather than turned into a
hollow report.

---

## Two lanes, kept apart by design

**Basic lane (no key).** A walk-in hands you a CSV; you hand back a report.
`runner.run_csv()` imports only the engine — it never touches the credential
store or the scheduler. If the entire keyed system were removed, this still works.

**Keyed lane (custodial).** A client onboards with an API key and a schedule.
Their key is encrypted into the registry; the scheduler runs their reports on
their cadence; their data and output live in their own workspace.

These lanes cannot interfere. The scheduler's entire universe is the registry, so
**a customer with no key cannot be seen, scheduled, or run.** No record = no run.
(Proven in `tests/test_operator.py::test_scheduler_cannot_see_unkeyed_customer`.)

---

## The intake flow

```
intake/onboarding/<client>.json   ← client fills out a form (incl. their key)
        │  process_onboarding()
        ▼
   • key encrypted into the registry (Fernet, at rest)
   • plaintext key SHREDDED from the form immediately
   • workspace provisioned:  clients/<id>/{inbox,output,archive}
   • logo (optional) copied into the workspace

clients/<id>/inbox/records.csv    ← client drops their data
        │  process_inbox()
        ▼
clients/<id>/output/report_*.html (+ .pdf)   ← their branded report
clients/<id>/archive/                         ← processed data filed away
```

### Security line on keys
A live API key only ever exists in plaintext for the few milliseconds between
"form read" and "encrypted into the store," then it is destroyed at the source.
Keys never travel by email and never sit in plaintext in a folder. The master
encryption key lives in the `OPERATOR_MASTER_KEY` environment variable — never in
the repo.

---

## Branding (all optional)

A client may supply a business name, email, phone, and a logo. The logo is
embedded into the report as a base64 data-URI, so each report is a single
self-contained file. **Every field is optional** — give everything, one thing, or
nothing, and the report always looks intentional. With nothing supplied it renders
exactly like the plain engine output.

---

## Quick start

```bash
pip install -r requirements.txt

# 1. one-time: generate a master key, put it in your environment (NEVER commit it)
python console.py keygen
export OPERATOR_MASTER_KEY='...the key it printed...'

# 2. onboard a client from a filled-in form, or directly:
python console.py add northwind "Northwind Outfitters" --key META-TOKEN --days mon,wed,fri

# 3. see your whole book
python console.py list

# 4. run one client now (manual), or everything due today:
python console.py run northwind
python console.py tick

# 5. basic no-key one-off (Fiverr $20 lane):
python console.py report some_export.csv "Client Name"
```

Unattended scheduling: run `scheduler.serve_forever(...)` as a launchd job
(Mac mini) or systemd service (VPS). It is **catch-up aware** — if the host was
asleep on a scheduled day, the missed run fires when it wakes, rather than being
silently skipped.

---

## Layout

```
report-operator/
├── engine/          # the report engine (analytics + branded HTML/PDF). Shared by both lanes.
├── registry.py      # encrypted client store (Fernet). The ONLY home of secrets.
├── runner.py        # run_csv() [basic lane]  |  run_client() [keyed lane]
├── scheduler.py     # catch-up-aware; iterates the registry ONLY
├── intake.py        # form scan-in, key shred, workspace provisioning, inbox→report
├── console.py       # operator CLI
├── deploy/          # host-agnostic deploy notes (laptop → mac mini → VPS)
└── tests/           # proves the isolation + intake guarantees
```

## The live Meta connector (off-ramp, not yet bolted on)
`runner.run_client()` takes a `fetch_fn(api_key, report_config, dest_csv)` — the
one seam where a live Meta Marketing API pull plugs in. Until then, clients bring
their own CSV via the inbox, which works today. When you add the connector, copy
this repo's own secret-handling pattern; nothing else changes.

## License

© 2026 Jared Jowett. All rights reserved.

This repository is shared publicly for portfolio and demonstration purposes only.
No license is granted to use, copy, modify, or distribute this software or its
source, in whole or in part, without express written permission from the author.
