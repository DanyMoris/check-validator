"""Журнал поступлений и сверка с чеком."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from checkvalidator.engine import analyse
from checkvalidator.extract import ExtractedFields, extract
from checkvalidator.ledger import (
    Ledger,
    parse_coverage_args,
    parse_csv_text,
    parse_income_args,
)
from checkvalidator.models import Verdict
from checkvalidator.profiles import ProfileRegistry

ROOT = Path(__file__).resolve().parent.parent
GENUINE = ROOT / "resources" / "genuine"
UNSIGNED = sorted((GENUINE / "VTB" / "Чек").glob("*.pdf"))


def test_parse_income_args() -> None:
    parsed = parse_income_args("/income 410 02.09.2026 13:02 кофе")
    assert parsed is not None
    amount, occurred, comment = parsed
    assert amount == 41000
    assert occurred == datetime(2026, 9, 2, 13, 2)
    assert "кофе" in comment

    spaced = parse_income_args("3 146,50 02.09.2026 16:33")
    assert spaced is not None
    assert spaced[0] == 314650
    assert spaced[1] == datetime(2026, 9, 2, 16, 33)


def test_parse_coverage_args_end_of_day() -> None:
    parsed = parse_coverage_args("/coverage 01.09.2026 11.09.2026")
    assert parsed is not None
    start, end = parsed
    assert start == datetime(2026, 9, 1, 0, 0)
    assert end == datetime(2026, 9, 11, 23, 59, 59)


def test_parse_csv_text() -> None:
    rows = parse_csv_text(
        "дата;сумма;описание\n02.09.2026 13:02;410,00;кофе\n06.09.2026 18:46;1788;магазин\n"
    )
    assert len(rows) == 2
    assert rows[0][1] == 41000
    assert rows[1][1] == 178800


def test_manual_income_confirms_without_coverage(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    fields = ExtractedFields(
        amount_kopecks=41000, occurred_at=datetime(2026, 9, 2, 13, 2)
    )
    match = ledger.match(fields, "aaa")
    assert not match.confirmed
    assert not match.covered

    ledger.add_entry(41000, datetime(2026, 9, 2, 12, 0), source="manual")
    match = ledger.match(fields, "aaa")
    assert match.confirmed
    assert match.hit is not None


def test_consume_prevents_reuse(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.add_entry(41000, datetime(2026, 9, 2, 13, 2), source="manual")
    fields = ExtractedFields(
        amount_kopecks=41000, occurred_at=datetime(2026, 9, 2, 13, 2)
    )
    first = ledger.match(fields, "file-a")
    assert first.hit is not None
    ledger.consume(first.hit.id, "file-a")
    again = ledger.match(fields, "file-a")
    assert again.confirmed
    stolen = ledger.match(fields, "file-b")
    assert stolen.reused
    assert not stolen.confirmed


def test_coverage_miss_is_not_a_hit(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.add_coverage(datetime(2026, 9, 1), datetime(2026, 9, 11, 23, 59, 59))
    fields = ExtractedFields(
        amount_kopecks=41000, occurred_at=datetime(2026, 9, 2, 13, 2)
    )
    match = ledger.match(fields, "aaa")
    assert match.covered
    assert not match.confirmed


def test_csv_import_creates_coverage(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_csv(
        "date,amount\n2026-09-02 13:02,410\n2026-09-06 18:46,1788\n"
    )
    assert result.count == 2
    assert result.coverage is not None
    fields = ExtractedFields(
        amount_kopecks=41000, occurred_at=datetime(2026, 9, 2, 13, 10)
    )
    assert ledger.match(fields, "x").confirmed


def test_csv_skips_debits_and_rejected(tmp_path: Path) -> None:
    text = (
        "Дата и время операции;Списание/Зачисление;Сумма в валюте счёта;Статус операции;Комментарий к операции\n"
        "02.09.2026 13:02;Зачисление;410,00;Выполнено;перевод\n"
        "03.09.2026 10:00;Списание;-80,00;Выполнено;кофе\n"
        "04.09.2026 11:00;Зачисление;50,00;Операция отклонена;отмена\n"
        "05.09.2026 12:00;Списание;-20,00;В обработке;hold\n"
    )
    rows = parse_csv_text(text)
    assert rows == [(datetime(2026, 9, 2, 13, 2), 41000, "перевод")]
    result = Ledger(tmp_path / "l.db").import_csv(text)
    assert result.count == 1
    assert result.debit_count == 2
    assert result.coverage is not None


def test_csv_and_pdf_same_day_do_not_duplicate(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.import_csv("дата;сумма\n02.09.2026 13:02;410\n06.09.2026 18:46;1788\n")
    from checkvalidator.statements import parse_statement_text

    parsed = parse_statement_text(
        """
Период выписки 01.09.2026 - 12.09.2026
Операции по счёту
02.09.2026 02.09.2026 410.00 RUB 410.00 RUB 0.00 RUB перевод
03.09.2026 03.09.2026 50.00 RUB 50.00 RUB 0.00 RUB другое
        """
    )
    assert parsed is not None
    for op in parsed.credits:
        assert op.amount_kopecks is not None and op.occurred_at is not None
        if ledger.find_same_day(op.amount_kopecks, op.occurred_at) is None:
            ledger.add_entry(op.amount_kopecks, op.occurred_at, source="statement-pdf")
    entries = ledger.list_entries(20)
    four_ten = [e for e in entries if e.amount_kopecks == 41000]
    assert len(four_ten) == 1
    assert four_ten[0].consumed_by is None
    fifty = [e for e in entries if e.amount_kopecks == 5000]
    assert len(fifty) == 1


def test_overlapping_coverage_merges_and_does_not_duplicate_entries(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    inner = ledger.import_csv(
        "дата;сумма\n05.09.2026 12:00;410\n06.09.2026 12:00;50\n",
        filename="inner.csv",
    )
    assert inner.count == 2
    assert len(ledger.list_coverage()) == 1
    outer = ledger.import_csv(
        "дата;сумма\n01.08.2026 10:00;100\n05.09.2026 12:00;410\n06.09.2026 12:00;50\n",
        filename="outer.csv",
    )
    assert outer.skipped == 2
    assert outer.count == 1
    amounts = {e.amount_kopecks for e in ledger.list_entries(20)}
    assert amounts == {41000, 5000, 10000}
    windows = ledger.list_coverage()
    assert len(windows) == 1
    assert windows[0].start_at == datetime(2026, 8, 1, 10, 0)
    nested = ledger.add_coverage(datetime(2026, 9, 5), datetime(2026, 9, 6, 23, 59, 59))
    assert len(ledger.list_coverage()) == 1
    assert nested.id == windows[0].id


def test_two_same_amount_same_day_are_both_kept(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_csv(
        "дата;сумма\n05.09.2026 12:00;410\n05.09.2026 18:00;410\n",
        filename="twins.csv",
    )
    assert result.count == 2
    assert result.skipped == 0
    assert len(ledger.list_entries()) == 2
    again = ledger.import_csv(
        "дата;сумма\n05.09.2026 13:00;410\n",
        filename="one.csv",
    )
    assert again.count == 0
    assert again.skipped == 1
    extra = ledger.import_csv(
        "дата;сумма\n05.09.2026 10:00;410\n05.09.2026 11:00;410\n05.09.2026 19:00;410\n",
        filename="three.csv",
    )
    assert extra.skipped == 2
    assert extra.count == 1
    assert len(ledger.list_entries()) == 3


def test_clear_removes_entries_and_coverage(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.import_csv("дата;сумма\n02.09.2026 13:02;410\n06.09.2026 18:46;1788\n")
    ledger.add_entry(1000, datetime(2026, 9, 3), source="manual")
    entries, coverage = ledger.clear()
    assert entries >= 3
    assert coverage >= 1
    assert ledger.counts() == (0, 0)
    assert ledger.list_imports() == []


def test_undo_import_removes_only_that_file(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    first = ledger.import_csv(
        "дата;сумма\n02.09.2026 13:02;410\n06.09.2026 18:46;1788\n",
        filename="a.csv",
    )
    second = ledger.import_csv(
        "дата;сумма\n03.09.2026 10:00;50\n04.09.2026 11:00;60\n",
        filename="b.csv",
    )
    assert first.import_id is not None and second.import_id is not None
    removed = ledger.undo_import(first.import_id)
    assert removed == (2, 1)
    left = ledger.list_entries(20)
    assert {e.amount_kopecks for e in left} == {5000, 6000}
    assert ledger.get_import(first.import_id) is None
    assert ledger.get_import(second.import_id) is not None


def test_find_import_by_hash(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    raw = "дата;сумма\n02.09.2026 13:02;410\n06.09.2026 18:46;1788\n"
    result = ledger.import_csv(raw, filename="vtb.csv")
    assert result.import_id is not None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    found = ledger.find_import_by_hash(digest)
    assert found is not None
    assert found.filename == "vtb.csv"
    assert found.entry_count == 2


VTB_FOLDER = ROOT / "resources" / "genuine" / "VTB" / "Выписка"
VTB_ACCOUNT = sorted(VTB_FOLDER.glob("*счёт*.pdf"))
VTB_CSV = sorted(VTB_FOLDER.glob("*.csv"))


@pytest.mark.skipif(not VTB_CSV, reason="нет CSV выписки ВТБ")
def test_real_vtb_csv_imports_only_completed_credits(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_csv(VTB_CSV[0].read_bytes())
    assert result.count >= 1
    assert result.debit_count >= 1
    assert result.coverage is not None
    amounts = [e.amount_kopecks for e in ledger.list_entries(100)]
    assert amounts
    assert all(a > 0 for a in amounts)


@pytest.mark.skipif(not (VTB_CSV and VTB_ACCOUNT), reason="нет CSV и PDF выписки ВТБ")
def test_real_csv_then_account_pdf_does_not_duplicate(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    csv_result = ledger.import_csv(VTB_CSV[0].read_bytes())
    pdf_result = ledger.import_pdf(VTB_ACCOUNT[0])
    assert pdf_result.is_statement
    assert pdf_result.skipped == csv_result.count
    after = ledger.list_entries(100)
    keys = [(e.amount_kopecks, e.occurred_at.date()) for e in after]
    assert len(keys) == len(set(keys))


@pytest.fixture(scope="module")
def registry() -> ProfileRegistry:
    reg = ProfileRegistry.load()
    if not len(reg):
        pytest.skip("нет профилей")
    return reg


@pytest.mark.skipif(not UNSIGNED, reason="нет чеков ВТБ")
def test_matching_income_confirms_unsigned_receipt(
    tmp_path: Path, registry: ProfileRegistry
) -> None:
    pdf = UNSIGNED[0]
    fields = extract(pdf)
    assert fields.usable_for_match
    ledger = Ledger(tmp_path / "l.db")
    assert fields.amount_kopecks is not None and fields.occurred_at is not None
    ledger.add_entry(fields.amount_kopecks, fields.occurred_at, source="test")
    report = analyse(pdf, registry, ledger=ledger, consume=True, doc_hash="doc-1")
    assert report.verdict is Verdict.CONFIRMED
    assert any(s.id == "ledger_match" and s.passed for s in report.signals)

    other = analyse(pdf, registry, ledger=ledger, consume=True, doc_hash="doc-2")
    assert other.verdict is Verdict.FORGED
    assert any(s.id == "ledger_reuse" for s in other.signals)


@pytest.mark.skipif(not UNSIGNED, reason="нет чеков ВТБ")
def test_coverage_without_entry_is_not_forged(
    tmp_path: Path, registry: ProfileRegistry
) -> None:
    pdf = UNSIGNED[0]
    fields = extract(pdf)
    assert fields.occurred_at is not None
    ledger = Ledger(tmp_path / "l.db")
    start = fields.occurred_at.replace(hour=0, minute=0, second=0)
    end = fields.occurred_at.replace(hour=23, minute=59, second=59)
    ledger.add_coverage(start, end, source="test")
    report = analyse(pdf, registry, ledger=ledger)
    assert report.verdict is Verdict.UNCONFIRMED
    assert report.verdict is not Verdict.FORGED
    assert any(s.id in {"ledger_missing", "ledger_outgoing"} for s in report.signals)


TBANK_INCOMING = sorted((GENUINE / "TBANK" / "Справка").glob("*.pdf"))
TBANK_RECEIPTS = sorted((GENUINE / "TBANK" / "Квитанция").glob("*.pdf"))


@pytest.mark.skipif(not TBANK_INCOMING, reason="нет справки Т-Банка")
def test_import_pdf_incoming_then_confirms(tmp_path: Path, registry: ProfileRegistry) -> None:
    pdf = TBANK_INCOMING[0]
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_pdf(pdf)
    assert result.count == 1
    assert result.direction == "incoming"
    assert result.coverage is None
    report = analyse(pdf, registry, ledger=ledger, consume=True, doc_hash="tbank-in")
    assert report.verdict is Verdict.CONFIRMED


@pytest.mark.skipif(not TBANK_RECEIPTS, reason="нет квитанций Т-Банка")
def test_import_pdf_warns_on_sender_receipt(tmp_path: Path) -> None:
    outgoing = [p for p in TBANK_RECEIPTS if p.name.startswith("receipt_")]
    if not outgoing:
        pytest.skip("нет исходящих квитанций Т-Банка")
    result = Ledger(tmp_path / "l.db").import_pdf(outgoing[0])
    assert result.count == 0
    assert result.direction == "outgoing"
    assert result.warning
