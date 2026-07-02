"""
Tests for the router (layer 3) — the spine that turns a declared type + a CSV
into the right report, honoring the customer's declaration and degrading safely.
"""
import os
import pytest
from engine.router import decide, render, looks_like_meta
from engine.profile import load_csv_rows


@pytest.fixture(scope="module")
def headers():
    return {
        "meta": list(load_csv_rows("engine/sample_fb_export.csv")[0].keys()),
        "sales": list(load_csv_rows("/tmp/testdata/sales.csv")[0].keys()),
        "survey": list(load_csv_rows("/tmp/testdata/survey.csv")[0].keys()),
    }


def test_sniff_detects_meta(headers):
    assert looks_like_meta(headers["meta"]) is True
    assert looks_like_meta(headers["sales"]) is False
    assert looks_like_meta(headers["survey"]) is False


def test_declared_type_is_authoritative(headers):
    # customer says generic on meta data -> respect it
    rendered, _ = decide("generic", headers["meta"])
    assert rendered == "generic"


def test_auto_routes_meta_to_meta(headers):
    rendered, _ = decide("auto", headers["meta"])
    assert rendered == "meta"


def test_auto_routes_unknown_to_generic(headers):
    rendered, _ = decide("auto", headers["sales"])
    assert rendered == "generic"


def test_wrong_meta_declaration_degrades_gracefully(headers):
    # declared meta but data isn't meta -> generic, with an explaining note
    rendered, note = decide("meta", headers["sales"])
    assert rendered == "generic"
    assert "weren't found" in note or "generic" in note


def test_unbuilt_specialized_type_falls_to_generic(headers):
    rendered, note = decide("sales", headers["sales"])
    assert rendered == "generic"
    assert "no specialized template yet" in note


def test_unknown_type_treated_as_auto(headers):
    rendered, _ = decide("banana", headers["meta"])
    assert rendered == "meta"   # falls back to auto, which sniffs meta


def test_render_meta_produces_sharp_template():
    r = render("engine/sample_fb_export.csv", "/tmp/rt_meta",
               report_type="auto", brand="NW", make_pdf=False)
    assert r.rendered_as == "meta"
    html = open(r.html_path, encoding="utf-8").read()
    assert "campaigns" in html.lower()


def test_render_generic_titled_by_declared_type():
    r = render("/tmp/testdata/sales.csv", "/tmp/rt_sales",
               report_type="sales", brand="Acme", make_pdf=False)
    assert r.rendered_as == "generic"
    assert "Sales Report" in open(r.html_path, encoding="utf-8").read()


def test_render_always_produces_html():
    for name in ("sales", "survey", "sensor"):
        r = render(f"/tmp/testdata/{name}.csv", f"/tmp/rt_{name}",
                   report_type="auto", brand="X", make_pdf=False)
        assert os.path.exists(r.html_path)
