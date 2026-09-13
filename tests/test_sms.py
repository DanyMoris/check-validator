"""Разбор SMS Сбера о зачислении."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from checkvalidator.ledger import Ledger
from checkvalidator.sms import looks_like_sber_sms, parse_sber_sms

ROOT = Path(__file__).resolve().parent.parent
SBER_SMS = ROOT / "resources" / "genuine" / "SBER" / "SMS" / "sms1.txt"

CREDIT = "СЧЁТ1234 09:43 Перевод по СБП из ВТБ +1р от И."
DEBIT = "СЧЁТ1234 09:43 Перевод по СБП -1р"


def test_parse_sber_credit_sms() -> None:
    parsed = parse_sber_sms(CREDIT, received_at=datetime(2026, 9, 13, 12, 0))
    assert parsed.found
    assert parsed.is_credit
    assert parsed.fields is not None
    assert parsed.fields.amount_kopecks == 100
    assert parsed.fields.occurred_at == datetime(2026, 9, 13, 9, 43)
    assert parsed.fields.payer == "И."


def test_sber_debit_sms_is_not_credit() -> None:
    parsed = parse_sber_sms(DEBIT, received_at=datetime(2026, 9, 13, 12, 0))
    assert parsed.found
    assert not parsed.is_credit
    assert parsed.fields is None


def test_sms_time_after_midnight_uses_previous_day() -> None:
    parsed = parse_sber_sms(
        "СЧЁТ1234 23:50 Перевод по СБП +410р",
        received_at=datetime(2026, 9, 13, 0, 10),
    )
    assert parsed.fields is not None
    assert parsed.fields.occurred_at == datetime(2026, 9, 12, 23, 50)
    assert parsed.fields.amount_kopecks == 41000


def test_import_sber_sms_writes_ledger(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_sms(CREDIT, received_at=datetime(2026, 9, 13, 12, 0))
    assert result.count == 1
    assert result.skipped == 0
    again = ledger.import_sms(CREDIT, received_at=datetime(2026, 9, 13, 12, 0))
    assert again.count == 0
    assert again.skipped == 1
    debit = ledger.import_sms(DEBIT, received_at=datetime(2026, 9, 13, 12, 0))
    assert debit.count == 0
    assert debit.warning


def test_real_sber_sms_sample_is_a_one_ruble_credit() -> None:
    assert SBER_SMS.exists()
    text = SBER_SMS.read_text(encoding="utf-8")
    assert looks_like_sber_sms(text)
    parsed = parse_sber_sms(text, received_at=datetime(2026, 9, 13, 12, 0))
    assert parsed.is_credit
    assert parsed.fields is not None
    assert parsed.fields.amount_kopecks == 100
    assert parsed.fields.occurred_at == datetime(2026, 9, 13, 9, 43)
