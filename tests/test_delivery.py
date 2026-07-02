"""Tests for the delivery off-ramp: outbox always works, email seam degrades."""
import os
import pytest
from engine.delivery import to_outbox, deliver, email_configured, send_email


def test_outbox_writes_clean_named_file(tmp_path):
    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF-fake")
    out = to_outbox(str(src), "acme", brand="Acme Co", outbox=str(tmp_path / "ob"))
    assert os.path.exists(out)
    assert "Acme_Co" in os.path.basename(out)
    assert out.endswith(".pdf")


def test_outbox_no_clobber_same_day(tmp_path):
    src = tmp_path / "r.pdf"; src.write_bytes(b"x")
    ob = str(tmp_path / "ob")
    a = to_outbox(str(src), "c", brand="B", outbox=ob)
    b = to_outbox(str(src), "c", brand="B", outbox=ob)
    assert a != b and os.path.exists(a) and os.path.exists(b)  # both kept


def test_email_inactive_without_creds(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    assert email_configured() is False
    # email_report must return False, not raise, when unconfigured
    assert send_email("x@y.com", "s", "b", attachments=[]) is False


def test_deliver_outbox_only_when_no_email(tmp_path, monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    src = tmp_path / "report.html"; src.write_text("<html>ok</html>")
    result = deliver(str(src), "acme", brand="Acme", to_email="",
                     outbox=str(tmp_path / "ob"))
    assert os.path.exists(result["outbox"])
    assert result["emailed"] is False
    assert result["email_active"] is False
