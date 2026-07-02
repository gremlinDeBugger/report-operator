"""
Tests for the scan-intake adapter (paper/scan -> structured rows -> engine).
Proves the pipeline plumbing independent of any vision model: extraction,
the confidence-flagging, the refuse-before-verify safety gate, correction,
and the hand-off to the engine.
"""
import os
import pytest
from engine.scan_intake import (
    process_scan, StubExtractor, VisionExtractor, apply_corrections,
    emit_rows, scan_to_csv, ScanNotVerified, CONFIDENCE_THRESHOLD,
)


def test_stub_extraction_produces_rows(tmp_path):
    form = process_scan("/fake/img.jpg", extractor=StubExtractor(),
                        work_dir=str(tmp_path))
    assert len(form.rows) == 3
    assert form.rows[0]["Cavity"] == "A"
    assert form.extractor == "stub"


def test_low_confidence_cells_are_flagged(tmp_path):
    form = process_scan("/fake/img.jpg",
                        extractor=StubExtractor(low_conf_field="Cycle_s"),
                        work_dir=str(tmp_path))
    assert len(form.needs_verify) == 1
    flagged = form.needs_verify[0]
    assert flagged.field == "r0:Cycle_s"
    assert flagged.confidence < CONFIDENCE_THRESHOLD


def test_emit_refuses_until_verified(tmp_path):
    form = process_scan("/fake/img.jpg",
                        extractor=StubExtractor(low_conf_field="Cycle_s"),
                        work_dir=str(tmp_path))
    with pytest.raises(ScanNotVerified):
        emit_rows(form)


def test_correction_clears_verify_and_updates_rows(tmp_path):
    form = process_scan("/fake/img.jpg",
                        extractor=StubExtractor(low_conf_field="Cycle_s"),
                        work_dir=str(tmp_path))
    form = apply_corrections(form, {"r0:Cycle_s": "99.9"})
    assert form.verified()
    assert form.rows[0]["Cycle_s"] == "99.9"   # correction propagated into row data
    assert emit_rows(form) == form.rows         # now emits


def test_fully_confident_scan_needs_no_verify(tmp_path):
    form = process_scan("/fake/img.jpg", extractor=StubExtractor(),
                        work_dir=str(tmp_path))
    assert form.verified()
    assert emit_rows(form) == form.rows


def test_scan_converges_to_csv_then_engine(tmp_path):
    form = process_scan("/fake/img.jpg", extractor=StubExtractor(),
                        work_dir=str(tmp_path))
    csv_path = scan_to_csv(form, str(tmp_path / "out.csv"))
    from engine.profile import profile_data, load_csv_rows
    prof = profile_data(load_csv_rows(csv_path))
    assert prof.n_rows == 3
    assert "Cycle_s" in prof.numeric_columns


def test_vision_extractor_raises_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        VisionExtractor().extract("/fake/img.jpg")


def test_failed_extraction_queues_not_crashes(tmp_path):
    class BoomExtractor(StubExtractor):
        name = "boom"
        def extract(self, image_path):
            raise ValueError("simulated read failure")
    # process_scan must catch and produce an empty flagged form, not crash
    form = process_scan("/fake/img.jpg", extractor=BoomExtractor(),
                        work_dir=str(tmp_path))
    assert form.rows == []
    assert "FAILED" in form.extractor
