"""Verification gate: no number reaches a client unless it traces to source."""
from engine.verify import verify_text, extract_claims

PAYLOAD = {
    "overall": {"spend": 12450.75, "roas": 3.42, "ctr_pct": 1.8,
                "cpa": 24.5, "conversions": 508, "conv_value": 42581.97},
    "campaigns": [
        {"name": "Spring", "spend": 8000.25, "roas": 4.1},
        {"name": "Brand", "spend": 4450.50, "roas": 2.2},
    ],
}


def test_grounded_text_passes():
    text = ("Spend of $12,451 returned 3.42x ROAS with 508 conversions. "
            "Spring led at 4.1x on $8,000 spend.")
    v = verify_text(text, PAYLOAD)
    assert v.ok, v.why()
    assert v.checked >= 5


def test_invented_number_fails():
    text = "Spend of $12,451 returned 3.42x ROAS, with revenue up 47% year over year."
    v = verify_text(text, PAYLOAD)          # 47% exists nowhere in the payload
    assert not v.ok
    assert any("47" in raw for raw, _ in v.unmatched)


def test_rounding_and_units_forgiven():
    # $12.5K for 12450.75 (rel tol), 3.4x for 3.42 (abs/rel), $42.6K for 42581.97
    v = verify_text("Roughly $12.5K spent at 3.4x, generating $42.6K in value.",
                    PAYLOAD)
    assert v.ok, v.why()


def test_list_length_and_share_are_legitimate():
    # 2 campaigns is a list length; small ints are exempt anyway. 64% is
    # Spring's share of spend (8000.25/12450.75 = 64.3%).
    v = verify_text("Spring took 64% of total spend across the campaigns.", PAYLOAD)
    assert v.ok, v.why()


def test_dates_years_and_prose_counts_exempt():
    claims = extract_claims("In Q3 2025, the top 3 campaigns held; see the "
                            "2024-06-30 baseline and the 7-day window.")
    assert claims == []


def test_no_numbers_trivially_ok():
    v = verify_text("Performance held steady and nothing needs attention.", PAYLOAD)
    assert v.ok and v.checked == 0


def test_never_raises_on_garbage():
    v = verify_text("$$$ 12,, x× 5%%", {"a": [1, 2]})
    assert isinstance(v.ok, bool)


def test_strict_mode_blocks_coincidental_percent():
    """With shares off, a % claim must exist in the payload itself."""
    payload = {"companies": [{"revenue_yoy_pct": 31.8,
                              "latest": {"revenue": 1_842_000_000,
                                         "net_margin_pct": 21.0}}]}
    ok = verify_text("Revenue rose 31.8% YoY with a 21% net margin.",
                     payload, allow_shares=False)
    bad = verify_text("Revenue soared 87% YoY.", payload, allow_shares=False)
    assert ok.ok and not bad.ok
