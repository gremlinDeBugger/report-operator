"""
Full-chain tests for the report operator.

Proves the things Jared specifically asked to be guaranteed:
  - a no-key (basic) customer can NEVER be seen/run by the scheduler
  - the plaintext API key is SHREDDED from the form after onboarding
  - the key lives only encrypted in the store
  - onboarding provisions the per-client workspace
  - a dropped CSV produces a branded report in the client's output folder
  - branding is fully optional (bare report still works)
  - one client's bad data doesn't stop the others (blast-radius containment)
"""
import os
import json
import base64
import shutil
import importlib
import pytest

# isolate every test run to a temp tree
import registry as registry_mod
import intake as intake_mod


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    # fresh master key + fresh store/paths per test
    monkeypatch.setenv("OPERATOR_MASTER_KEY", registry_mod.generate_master_key())
    store = tmp_path / "clients.json"
    monkeypatch.setattr(registry_mod, "DEFAULT_STORE", str(store))
    monkeypatch.setattr(intake_mod, "ONBOARD_DIR", str(tmp_path / "intake" / "onboarding"))
    monkeypatch.setattr(intake_mod, "ONBOARD_ARCHIVE", str(tmp_path / "intake" / "onboarding" / "_arch"))
    monkeypatch.setattr(intake_mod, "CLIENTS_ROOT", str(tmp_path / "clients"))
    os.makedirs(intake_mod.ONBOARD_DIR, exist_ok=True)
    # sample CSV the engine can parse
    sample_src = os.path.join(os.path.dirname(__file__), "..", "engine", "sample_fb_export.csv")
    return {"tmp": tmp_path, "store": str(store), "sample": os.path.abspath(sample_src)}


def _drop_form(tmp, **fields):
    form = {"client_id": "acme", "brand": "Acme", "api_key": "SECRET-KEY-123",
            "schedule": "mon,wed,fri"}
    form.update(fields)
    p = os.path.join(intake_mod.ONBOARD_DIR, f"{form['client_id']}.json")
    with open(p, "w") as f:
        json.dump(form, f)
    return p


def test_onboarding_encrypts_and_shreds_key(env):
    form_path = _drop_form(env["tmp"])
    r = registry_mod.Registry(env["store"])
    done = intake_mod.process_onboarding(r)
    assert "acme" in done

    # the original form file was archived; find it
    arch = intake_mod.ONBOARD_ARCHIVE
    archived = [os.path.join(arch, f) for f in os.listdir(arch)]
    assert archived, "form should be archived"
    body = open(archived[0], encoding="utf-8").read()
    # PLAINTEXT KEY MUST BE GONE from the archived form
    assert "SECRET-KEY-123" not in body
    assert "SHREDDED" in body

    # key is recoverable ONLY via decryption from the store
    r2 = registry_mod.Registry(env["store"])
    assert r2.decrypt_key_for("acme") == "SECRET-KEY-123"
    # and is NOT stored in plaintext anywhere in the store file
    assert "SECRET-KEY-123" not in open(env["store"], encoding="utf-8").read()


def test_onboarding_provisions_workspace(env):
    _drop_form(env["tmp"])
    r = registry_mod.Registry(env["store"])
    intake_mod.process_onboarding(r)
    base = intake_mod.client_dir("acme")
    for sub in ("inbox", "output", "archive"):
        assert os.path.isdir(os.path.join(base, sub))


def test_scheduler_cannot_see_unkeyed_customer(env):
    """THE core guarantee: a basic customer with no registry record is invisible
    to the scheduler. No record => not in active_clients => never due => never run."""
    import scheduler
    r = registry_mod.Registry(env["store"])
    # registry is empty — a 'basic customer' exists only as a CSV on disk, nowhere here
    assert r.active_clients() == []
    assert scheduler.due_clients(r) == []
    results = scheduler.run_due(r)
    assert results == []   # nothing ran, because nothing COULD


def test_dropped_csv_makes_branded_report(env):
    # logo file on disk for branding
    logo = env["tmp"] / "logo.png"
    logo.write_bytes(TINY_PNG)
    _drop_form(env["tmp"], business_name="Acme Media", email="hi@acme.com",
               phone="555-1000", logo_path=str(logo))
    r = registry_mod.Registry(env["store"])
    intake_mod.process_onboarding(r)

    # drop their records into their inbox
    base = intake_mod.client_dir("acme")
    shutil.copy(env["sample"], os.path.join(base, "inbox", "may.csv"))

    r2 = registry_mod.Registry(env["store"])
    results = intake_mod.process_inbox(r2)
    assert any(ok for (_, ok, _) in results)

    outs = os.listdir(os.path.join(base, "output"))
    html = [o for o in outs if o.endswith(".html")][0]
    body = open(os.path.join(base, "output", html), encoding="utf-8").read()
    assert "Acme Media" in body
    assert "hi@acme.com" in body
    assert "data:image/png;base64" in body         # logo embedded
    # csv was archived out of the inbox
    assert os.listdir(os.path.join(base, "inbox")) == []


def test_branding_fully_optional(env):
    # onboard with NO branding fields at all
    _drop_form(env["tmp"], client_id="bare", brand="BareCo")
    r = registry_mod.Registry(env["store"])
    intake_mod.process_onboarding(r)
    base = intake_mod.client_dir("bare")
    shutil.copy(env["sample"], os.path.join(base, "inbox", "d.csv"))
    intake_mod.process_inbox(registry_mod.Registry(env["store"]))
    out = os.listdir(os.path.join(base, "output"))
    html = [o for o in out if o.endswith(".html")][0]
    body = open(os.path.join(base, "output", html), encoding="utf-8").read()
    assert "<h1>BareCo</h1>" in body
    assert '<img class="brand-logo"' not in body     # no logo element
    assert 'class="contact"' not in body             # no contact block


def test_one_bad_client_does_not_block_others(env):
    # good client
    _drop_form(env["tmp"], client_id="good", brand="Good")
    # bad client
    _drop_form(env["tmp"], client_id="bad", brand="Bad")
    r = registry_mod.Registry(env["store"])
    intake_mod.process_onboarding(r)

    g = intake_mod.client_dir("good")
    b = intake_mod.client_dir("bad")
    shutil.copy(env["sample"], os.path.join(g, "inbox", "ok.csv"))
    # malformed CSV for the bad client
    with open(os.path.join(b, "inbox", "broken.csv"), "w") as f:
        f.write("not,a,valid\nexport,file,at all\n")

    results = intake_mod.process_inbox(registry_mod.Registry(env["store"]))
    by_client = {cid: ok for (cid, ok, _) in results}
    assert by_client.get("good") is True
    assert by_client.get("bad") is False
    # good client's report exists despite bad client failing
    assert any(o.endswith(".html") for o in os.listdir(os.path.join(g, "output")))
