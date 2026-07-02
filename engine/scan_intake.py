"""
scan_intake.py — Inbound adapter: turn a SCANNED/PHOTOGRAPHED form into the same
structured rows the engine already eats. A peer to the CSV-drop lane.

Why this exists: not everyone has clean CSV data. A huge amount of real-world
data lives on paper — checklists, log sheets, inspection forms filled out by
hand. This adapter lets the platform ingest that: photo in, structured rows out,
then straight into the existing profile -> route -> render pipeline.

PIPELINE (four stages):
    1. image intake   — a scan/photo lands in a scan inbox
    2. extract        — an Extractor reads the image into rows  ← the seam
    3. verify         — low-confidence fields flagged for a human to confirm/fix
    4. emit           — clean rows handed to the engine

THE EXTRACTOR IS PLUGGABLE. The pipeline doesn't care HOW the image becomes rows:
  - StubExtractor      : returns canned rows — for testing the plumbing
  - VisionExtractor    : calls a vision model (needs API key) — production
  - (future) OCRExtractor for clean printed forms
This is the same "build the pipeline complete, activate the model call with a key
later" pattern as the Meta connector and the AI insight. The plumbing is proven
independent of how good the reading is.

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

log = logging.getLogger("scan_intake")


# --------------------------------------------------------------------------- #
# Extraction result types
# --------------------------------------------------------------------------- #
@dataclass
class Cell:
    """One extracted value plus how confident the extractor is in it."""
    field: str
    value: str
    confidence: float = 1.0      # 0..1; below threshold -> flagged for verify


@dataclass
class ExtractedForm:
    """The structured result of reading one form image."""
    source_image: str
    rows: list[dict] = field(default_factory=list)        # the engine-ready data
    cells: list[Cell] = field(default_factory=list)       # flat list for verify UI
    needs_verify: list[Cell] = field(default_factory=list)  # low-confidence subset
    extractor: str = ""

    def verified(self) -> bool:
        return not self.needs_verify


# --------------------------------------------------------------------------- #
# Pluggable extractors
# --------------------------------------------------------------------------- #
class Extractor:
    """Interface: read an image path, return an ExtractedForm."""
    name = "base"

    def extract(self, image_path: str) -> ExtractedForm:
        raise NotImplementedError


class StubExtractor(Extractor):
    """
    Test extractor. Returns canned rows so the WHOLE pipeline (intake -> verify ->
    emit -> report) can be proven without any vision model or API key. The canned
    data can include a deliberately low-confidence cell to exercise the verify path.
    """
    name = "stub"

    def __init__(self, rows=None, low_conf_field: str | None = None):
        self._rows = rows or [
            {"Time": "08:00", "Cavity": "A", "Cycle_s": "22.5", "Defects": "0"},
            {"Time": "09:00", "Cavity": "A", "Cycle_s": "22.7", "Defects": "1"},
            {"Time": "10:00", "Cavity": "B", "Cycle_s": "23.1", "Defects": "0"},
        ]
        self._low = low_conf_field

    def extract(self, image_path: str) -> ExtractedForm:
        cells, needs = [], []
        for i, row in enumerate(self._rows):
            for k, v in row.items():
                conf = 0.55 if (self._low and k == self._low and i == 0) else 0.98
                c = Cell(field=f"r{i}:{k}", value=str(v), confidence=conf)
                cells.append(c)
                if conf < CONFIDENCE_THRESHOLD:
                    needs.append(c)
        return ExtractedForm(source_image=image_path, rows=list(self._rows),
                             cells=cells, needs_verify=needs, extractor=self.name)


class VisionExtractor(Extractor):
    """
    Production extractor — hands the image to a vision model and asks for the grid
    as structured JSON rows. Needs ANTHROPIC_API_KEY (or another vision provider).
    This is the activatable seam; until a key is present it raises, and callers
    fall back to a stub or queue the image for manual entry.

    The form_spec describes the known grid (row labels, column headers) so the
    model knows what it's reading — accuracy is far higher with a spec than asking
    it to guess the layout cold.
    """
    name = "vision"

    def __init__(self, form_spec: dict | None = None, model: str | None = None):
        self.form_spec = form_spec or {}
        self.model = model or os.environ.get("VISION_MODEL", "claude-opus-4-8")

    def extract(self, image_path: str) -> ExtractedForm:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VisionExtractor needs ANTHROPIC_API_KEY. No key set — activate "
                "this seam with a key, or use StubExtractor / manual entry.")
        import base64, mimetypes, anthropic
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        spec_hint = (f"The form layout: {json.dumps(self.form_spec)}. "
                     if self.form_spec else "")
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=self.model, max_tokens=2000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": mime, "data": b64}},
                {"type": "text", "text":
                 (spec_hint + "Read this form into JSON: an array of row objects "
                  "with consistent keys. For each value also give a confidence 0-1. "
                  "Return ONLY JSON: {\"rows\":[...], \"cells\":[{\"field\":..,"
                  "\"value\":..,\"confidence\":..}]}. Use empty string for blank "
                  "cells. Do not invent values you cannot read.")}]}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
        cells = [Cell(c.get("field", ""), str(c.get("value", "")),
                      float(c.get("confidence", 1.0))) for c in data.get("cells", [])]
        needs = [c for c in cells if c.confidence < CONFIDENCE_THRESHOLD]
        return ExtractedForm(source_image=image_path, rows=data.get("rows", []),
                             cells=cells, needs_verify=needs, extractor=self.name)


CONFIDENCE_THRESHOLD = 0.80   # cells below this get flagged for human verify


# --------------------------------------------------------------------------- #
# The intake pipeline
# --------------------------------------------------------------------------- #
def process_scan(image_path: str, extractor: Extractor | None = None,
                 work_dir: str = "scan_work") -> ExtractedForm:
    """
    Stage 1-2: read an image into an ExtractedForm. Writes a verify-pending JSON
    next to the work dir so a human (or the verify step) can confirm/fix before
    the rows are emitted. Never raises on a bad extractor result here — a failed
    read becomes an empty form flagged for full manual entry.
    """
    os.makedirs(work_dir, exist_ok=True)
    extractor = extractor or StubExtractor()
    try:
        form = extractor.extract(image_path)
    except Exception as e:
        log.error("extraction failed for %s (%s) — queuing for manual entry",
                  image_path, e)
        form = ExtractedForm(source_image=image_path, rows=[], cells=[],
                             needs_verify=[], extractor=f"{extractor.name}:FAILED")

    # write a verify-pending record
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    pending = os.path.join(work_dir, f"pending_{stamp}.json")
    with open(pending, "w", encoding="utf-8") as f:
        json.dump({"source_image": form.source_image, "extractor": form.extractor,
                   "rows": form.rows,
                   "cells": [asdict(c) for c in form.cells],
                   "needs_verify": [asdict(c) for c in form.needs_verify]},
                  f, indent=2)
    log.info("extracted %s via %s: %d rows, %d cells, %d need verify -> %s",
             os.path.basename(image_path), form.extractor, len(form.rows),
             len(form.cells), len(form.needs_verify), pending)
    return form


def apply_corrections(form: ExtractedForm, corrections: dict[str, str]) -> ExtractedForm:
    """
    Stage 3: a human confirms/fixes flagged cells. `corrections` maps a cell field
    id ('r0:Cycle_s') to the corrected value. Applies them to both the flat cells
    and the row data, then clears the needs_verify list.
    """
    by_field = {c.field: c for c in form.cells}
    for fid, newval in corrections.items():
        if fid in by_field:
            by_field[fid].value = str(newval)
            by_field[fid].confidence = 1.0
            # propagate into the row data: field id is 'r{idx}:{key}'
            try:
                ridx_s, key = fid.split(":", 1)
                ridx = int(ridx_s[1:])
                if 0 <= ridx < len(form.rows):
                    form.rows[ridx][key] = str(newval)
            except (ValueError, IndexError):
                pass
    form.needs_verify = [c for c in form.cells if c.confidence < CONFIDENCE_THRESHOLD]
    return form


def emit_rows(form: ExtractedForm, require_verified: bool = True) -> list[dict]:
    """
    Stage 4: hand the clean rows to the engine. By default refuses to emit while
    cells still need verification — so unverified handwriting can't silently
    become an official report.
    """
    if require_verified and not form.verified():
        raise ScanNotVerified(
            f"{len(form.needs_verify)} cell(s) still need verification before "
            f"this scan can become a report")
    return form.rows


class ScanNotVerified(Exception):
    pass


def scan_to_csv(form: ExtractedForm, csv_path: str) -> str:
    """Convenience: write emitted rows to a CSV so the scan lane converges with
    the existing CSV lane — the rest of the pipeline is then identical."""
    import csv
    rows = emit_rows(form)
    if not rows:
        raise ValueError("no rows to write")
    keys = list(rows[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    return csv_path
