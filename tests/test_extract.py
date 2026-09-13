"""Разбор полей чека и контрольные суммы реквизитов."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from checkvalidator.extract import (
    extract,
    extract_from_text,
    extract_pdf_text,
    format_rub,
    parse_amount,
    parse_datetime,
)
from checkvalidator.requisites import account_key_ok, inn_checksum_ok
from checkvalidator.statements import looks_like_statement, parse_statement

ROOT = Path(__file__).resolve().parent.parent
GENUINE = ROOT / "resources" / "genuine"
GENUINE_FILES = sorted(GENUINE.rglob("*.pdf"))
RECEIPT_FILES = [p for p in GENUINE_FILES if p.parent.name != "Выписка"]
SPRAVKA_FILES = [p for p in GENUINE_FILES if p.parent.name == "Справка"]


def test_parse_amount_formats() -> None:
    assert parse_amount("410") == 41000
    assert parse_amount("410,00") == 41000
    assert parse_amount("410.00 RUR") == 41000
    assert parse_amount("3 146,50 ₽") == 314650
    assert parse_amount("3 146,50 i") == 314650
    assert parse_amount("45,00 ₽") == 4500
    assert parse_amount("0,45") == 45
    assert parse_amount("9 000") == 900000
    assert parse_amount("0 RUR") == 0
    assert parse_amount("нет") is None


def test_parse_datetime_formats() -> None:
    assert parse_datetime("02.09.2026 13:02") == datetime(2026, 9, 2, 13, 2)
    assert parse_datetime("02.09.2026, 13:02:42") == datetime(2026, 9, 2, 13, 2, 42)
    assert parse_datetime("18 мая 2026 в 20:11") == datetime(2026, 5, 18, 20, 11)
    assert parse_datetime("11 декабря 2025 06:39:01") == datetime(2025, 12, 11, 6, 39, 1)
    assert parse_datetime("2026-09-02T13:02:00") == datetime(2026, 9, 2, 13, 2)


def test_format_rub() -> None:
    assert format_rub(41000) == "410,00 ₽"
    assert format_rub(314650) == "3 146,50 ₽"
    assert format_rub(45) == "0,45 ₽"


def test_inn_checksum() -> None:
    assert inn_checksum_ok("7707083893")  # Сбер
    assert inn_checksum_ok("7702070139")  # ВТБ
    assert not inn_checksum_ok("7702070138")
    assert not inn_checksum_ok("123")


def test_account_key() -> None:
    bik = "044525225"
    # Собрать счёт с верным ключом в 9-м разряде.
    digits = list("40817810000000000000")
    digits[8] = "0"
    from checkvalidator.requisites import _account_mod10

    remainder = _account_mod10(bik[-3:], "".join(digits))
    digits[8] = str((10 - remainder) % 10)
    account = "".join(digits)
    assert account_key_ok(bik, account)
    digits[8] = str((int(digits[8]) + 1) % 10)
    assert not account_key_ok(bik, "".join(digits))


def test_extract_from_alfa_like_text() -> None:
    text = (
        "Квитанция о переводе по СБП\n"
        "Сумма перевода\n"
        "456 RUR\n"
        "Комиссия\n"
        "0 RUR\n"
        "Дата и время перевода\n"
        "06.09.2026 20:31:02 мск\n"
        "Номер операции\n"
        "C123\n"
    )
    fields = extract_from_text(text)
    assert fields.amount_kopecks == 45600
    assert fields.commission_kopecks == 0
    assert fields.occurred_at == datetime(2026, 9, 6, 20, 31, 2)
    assert fields.operation_id == "C123"


def test_extract_commission_math_from_same_line() -> None:
    text = "Итого 3 146,50 i\nСумма 3 100 i\nКомиссия 46,50 i\n02.09.2026 16:33:20\n"
    fields = extract_from_text(text)
    assert fields.amount_kopecks == 310000
    assert fields.total_kopecks == 314650
    assert fields.commission_kopecks == 4650
    assert fields.occurred_at == datetime(2026, 9, 2, 16, 33, 20)


def test_bez_komissii_is_zero() -> None:
    text = (
        "02.09.2026 16:33:20\n"
        "Итого 2 500 i\n"
        "Сумма 2 500 i\n"
        "Комиссия Без комиссии\n"
        "Квитанция № 1-1-1-1-1\n"
    )
    fields = extract_from_text(text)
    assert fields.amount_kopecks == 250000
    assert fields.total_kopecks == 250000
    assert fields.commission_kopecks == 0


def test_incoming_direction() -> None:
    from checkvalidator.extract import document_direction

    assert (
        document_direction("Пополнение. Система быстрых платежей\nСчет зачисления *1234")
        == "incoming"
    )
    assert (
        document_direction("По вопросам зачисления обращайтесь к получателю\nКвитанция № 1")
        == "outgoing"
    )


def test_extract_prefers_operation_date_over_formed_date() -> None:
    text = (
        "Сформирована\n11.09.2026 20:49 мск\n"
        "Сумма перевода\n1000 RUR\n"
        "Дата и время перевода\n06.09.2026 20:31:02 мск\n"
    )
    fields = extract_from_text(text)
    assert fields.occurred_at == datetime(2026, 9, 6, 20, 31, 2)


def test_sber_sbp_receipt_reads_date_after_check_label() -> None:
    text = (
        "Операция\n"
        "Сумма перевода\n"
        "1,40 ₽\n"
        "Комиссия\n"
        "0,00 ₽\n"
        "Чек по операции\n"
        "13 сентября 2026 09:43:01 (МСК)\n"
        "Номер операции в СБП\n"
        "A111K222\n"
    )
    fields = extract_from_text(text)
    assert fields.amount_kopecks == 140
    assert fields.occurred_at == datetime(2026, 9, 13, 9, 43, 1)
    assert fields.operation_id == "A111K222"


@pytest.mark.skipif(not RECEIPT_FILES, reason="нет образцов в resources/genuine")
@pytest.mark.parametrize("pdf", RECEIPT_FILES, ids=lambda p: p.name)
def test_genuine_pdfs_yield_amount_and_date(pdf: Path) -> None:
    fields = extract(pdf)
    assert fields.amount_kopecks is not None, "не прочиталась сумма"
    assert fields.amount_kopecks > 0
    assert fields.occurred_at is not None, "не прочиталась дата"


@pytest.mark.skipif(not GENUINE_FILES, reason="нет образцов")
def test_sber_service_check_commission_adds_up() -> None:
    files = sorted((GENUINE / "SBER" / "Чек").glob("*.pdf"))
    service = [p for p in files if extract(p).total_kopecks is not None]
    if not service:
        pytest.skip("нет чека Сбера с итогом")
    fields = extract(service[0])
    assert fields.amount_kopecks is not None
    assert fields.commission_kopecks is not None
    assert fields.total_kopecks == fields.amount_kopecks + fields.commission_kopecks
    if fields.inn:
        assert inn_checksum_ok(fields.inn)
    if fields.bik and fields.account:
        assert account_key_ok(fields.bik, fields.account)


def test_sber_payment_account_spravka_reads_operation_not_header() -> None:
    text = (
        "ПАО «Сбербанк» сообщает, что указанная ниже операция зачисления была\n"
        "совершена по платёжному счёту.\n"
        "Операция совершена\n"
        "5 апреля 2026 в 14:30\n"
        "Статус операции\n"
        "Исполнена\n"
        "Сумма в валюте операции\n"
        "410,00 руб.\n"
        "Сумма в валюте счета\n"
        "410,00 руб.\n"
        "Тип операции\n"
        "Перевод по СБП\n"
        "Справка по операции\n"
    )
    fields = extract_from_text(text)
    assert fields.amount_kopecks == 41000
    assert fields.occurred_at == datetime(2026, 4, 5, 14, 30)
    assert not looks_like_statement(text)


@pytest.mark.skipif(not SPRAVKA_FILES, reason="нет справок в корпусе")
@pytest.mark.parametrize("pdf", SPRAVKA_FILES, ids=lambda p: p.name)
def test_spravki_have_amount_date_and_are_not_statements(pdf: Path) -> None:
    fields = extract(pdf)
    assert fields.usable_for_match
    assert fields.amount_kopecks is not None and fields.amount_kopecks > 0
    assert fields.occurred_at is not None
    text = extract_pdf_text(pdf)
    assert not looks_like_statement(text)
    assert parse_statement(pdf) is None
