"""
verify.py — The QC gate between AI generation and delivery.

Every AI-written insight is generated FROM a metrics payload (a JSON of numbers
computed deterministically from the source data). This module closes the loop:
after generation, every numeric claim in the text is extracted and checked back
against that same payload. A number the model cannot justify from the source
figures fails verification.

Policy on failure is decided by the CALLER (insight.py): the standard move is to
discard the AI text and ship the deterministic summary instead — so a report can
never reach a client carrying a number that isn't in the data. The report always
generates; only the prose is swapped.

What counts as a match:
  - any numeric value in the payload (searched recursively), within tolerance
  - common formatting variants: the model may round ($1,234.56 -> "$1,235",
    "about $1.2K", "1.2M"), so both a relative and an absolute tolerance apply
  - derived values the model may legitimately state: list lengths ("across 8
    campaigns"), and pairwise percent shares of payload values ("42% of spend")

What is exempt (too ambiguous to pin to a source figure):
  - years (1990–2039) and date-like strings
  - small integers 0–10 ("top 3", "two of the campaigns") — prose counting,
    not data claims
  - ordinals swallowed by the extractor ("Q3", "7-day") via unit filtering

Author: Jared Jowett (GremlinHunter)
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

log = logging.getLogger("verify")

# $1,234.56 | 1,234 | 12.5% | 2.5× or 2.5x | 1.2M / 3.4K / 1.1B | plain 42
_NUM_RE = re.compile(r"""
    (?P<currency>\$)?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?   # 1,234 or 1,234.56
          |\d+\.\d+                        # 12.5
          |\d+)                            # 42
    \s*(?P<suffix>[KkMmBb](?![a-zA-Z])|%|×|x(?![a-zA-Z]))?
""", re.VERBOSE)

_SUFFIX_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_WORD_MULT = {"thousand": 1_000, "million": 1_000_000,
              "billion": 1_000_000_000, "trillion": 1_000_000_000_000}
_WORD_RE = re.compile(r"^\s*(thousand|million|billion|trillion)\b", re.IGNORECASE)

# "Q3", "FY2025", "2024-06-30", "7-day" — patterns whose digits are not claims
_DATEY_RE = re.compile(r"""(?:\bQ[1-4]\b|\bFY\s?\d{2,4}\b|\b\d{4}-\d{2}(?:-\d{2})?\b
                            |\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d+-(?:day|week|month|year)\b|'\d{2}\b|\bQ[1-4]\s?'?\d{2}\b|\bFY\s?'?\d{2}\b)""",
                       re.VERBOSE | re.IGNORECASE)


@dataclass
class VerifyResult:
    ok: bool
    checked: int                 # numeric claims examined
    unmatched: list = field(default_factory=list)   # [(raw_text, value), ...]

    def why(self) -> str:
        if self.ok:
            return f"all {self.checked} numeric claims matched source figures"
        bad = ", ".join(f"'{raw}'" for raw, _ in self.unmatched[:4])
        return (f"{len(self.unmatched)} of {self.checked} numeric claims not "
                f"found in source figures: {bad}")


def extract_claims(text: str) -> list[tuple[str, float]]:
    """Pull (raw, value) numeric claims out of prose, skipping dates/years/
    small prose counts."""
    scrubbed = _DATEY_RE.sub(" ", text or "")
    claims = []
    for m in _NUM_RE.finditer(scrubbed):
        raw = m.group(0).strip()
        val = float(m.group("num").replace(",", ""))
        suffix = (m.group("suffix") or "").lower()
        if suffix in _SUFFIX_MULT:
            val *= _SUFFIX_MULT[suffix]
        elif not suffix:
            wm = _WORD_RE.match(scrubbed[m.end():])
            if wm:
                val *= _WORD_MULT[wm.group(1).lower()]
                suffix = wm.group(1).lower()   # treat as scaled, not a bare int
        # years read as plain numbers are not data claims
        if 1990 <= val <= 2039 and val == int(val) and not m.group("currency") \
                and suffix in ("", None):
            continue
        # small prose counts ("top 3", "two of five") are exempt
        if val <= 10 and val == int(val) and not m.group("currency") \
                and suffix in ("", None):
            continue
        claims.append((raw, val))
    return claims


def _walk(obj, out: list[float]):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, (list, tuple)):
        out.append(float(len(obj)))          # "across 8 campaigns"
        for v in obj:
            _walk(v, out)


def candidate_values(payload: dict, *, allow_shares: bool = True) -> list[float]:
    """Every number the model could legitimately cite: payload values, list
    lengths, and (optionally) pairwise percent shares of the larger values.

    allow_shares=False is STRICT mode: percent claims must appear in the
    payload itself. Use it when the payload already carries its own computed
    percentages (margins, YoY/QoQ growth) — as the financial payload does —
    so a made-up "87% YoY" can't coincidentally match some ratio of two
    unrelated figures."""
    vals: list[float] = []
    _walk(payload, vals)
    if not allow_shares:
        return vals
    # percent shares — "X was 42% of Y" for meaningful magnitudes only
    big = [v for v in vals if abs(v) > 1]
    shares = []
    for a in big:
        for b in big:
            if b and a != b and abs(a) < abs(b):
                shares.append(a / b * 100.0)
    return vals + shares


def _matches(claim: float, cand: float, rel_tol: float, abs_tol: float) -> bool:
    if abs(claim - cand) <= abs_tol:
        return True
    if cand and abs(claim - cand) / abs(cand) <= rel_tol:
        return True
    return False


def verify_text(text: str, payload: dict, *,
                rel_tol: float = 0.015, abs_tol: float = 0.51,
                allow_shares: bool = True) -> VerifyResult:
    """
    Check every numeric claim in `text` against `payload`. Tolerances allow the
    rounding the model is instructed to do (abs 0.51 forgives cents-to-dollar
    rounding; rel 1.5% forgives "$1.2M" for $1,187,400).
    Never raises.
    """
    try:
        claims = extract_claims(text)
        if not claims:
            return VerifyResult(ok=True, checked=0)
        cands = candidate_values(payload or {}, allow_shares=allow_shares)
        unmatched = [(raw, v) for raw, v in claims
                     if not any(_matches(v, c, rel_tol, abs_tol) for c in cands)]
        res = VerifyResult(ok=not unmatched, checked=len(claims), unmatched=unmatched)
        if not res.ok:
            log.warning("verification FAILED — %s", res.why())
        return res
    except Exception as e:                                   # pragma: no cover
        log.warning("verifier error (%s) — treating as unverified", e)
        return VerifyResult(ok=False, checked=0, unmatched=[("<verifier-error>", 0.0)])
