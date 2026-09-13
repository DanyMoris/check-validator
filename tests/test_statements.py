"""Разбор выписок ВТБ, Альфы, Сбера и Т-Банка за период."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from checkvalidator.extract import ExtractedFields
from checkvalidator.ledger import Ledger
from checkvalidator.statements import looks_like_statement, parse_statement, parse_statement_text

ROOT = Path(__file__).resolve().parent.parent
VTB_FOLDER = ROOT / "resources" / "genuine" / "VTB" / "Выписка"
VTB_CARD = sorted(VTB_FOLDER.glob("*карт*.pdf"))
VTB_ACCOUNT = sorted(VTB_FOLDER.glob("*счёт*.pdf"))
ALFA_STMT = sorted((ROOT / "resources" / "genuine" / "ALFA" / "Выписка").glob("*.pdf"))
SBER_STMT = sorted((ROOT / "resources" / "genuine" / "SBER" / "Выписка").glob("*.pdf"))
SBER_SPRAVKA = sorted((ROOT / "resources" / "genuine" / "SBER" / "Справка").glob("*.pdf"))
TBANK_STMT = sorted((ROOT / "resources" / "genuine" / "TBANK" / "Выписка").glob("*.pdf"))
TBANK_SPRAVKA = sorted((ROOT / "resources" / "genuine" / "TBANK" / "Справка").glob("*.pdf"))

SAMPLE = """
Период выписки 01.09.2026 - 12.09.2026
Баланс на начало периода 100.00 RUB
Операции по карте
Дата и время операции Дата обработки банком
10.09.2026
16:49:22 12.09.2026 0.0 RUB -80.0 0.0 RUB Оплата товаров и услуг.
05.09.2026
12:00:00 05.09.2026 0.0 RUB +410.0 0.0 RUB Входящий перевод СБП.
01.09.2026
09:00:00 01.09.2026 0.0 RUB -20.0 0.0 RUB Комиссия.
"""


ACCOUNT = """
Период выписки 12.08.2026 - 12.09.2026
Баланс на начало периода 100.00 RUB Поступления 460.00 RUB
Операции по счёту
Дата и время операции Дата обработки банком
05.09.2026 05.09.2026 410.00 RUB 410.00 RUB 0.00 RUB Переводы через СБП.
10.09.2026 10.09.2026 -80.00 RUB 0.00 RUB -80.00 RUB
Оплата товаров и услуг.
01.09.2026 01.09.2026 50.00 RUB 50.00 RUB 0.00 RUB Входящий перевод.
08.08.2026 08.08.2026 2500.00 RUB 2500.00 RUB 0.00 RUB Крупный перевод.
"""


ALFA = """
Выписка по счету
АО «АЛЬФА-БАНК»
За период с 01.09.2026 по 12.09.2026
Входящий остаток 1 000,00 RUR
Поступления 1 644,00 RUR
Расходы 130,00 RUR
Операции по счету
Дата проводки Код операции Описание Сумма
в валюте счета
05.09.2026 C111 Перевод через Систему быстрых платежей от +7999.
Без НДС.
410,00 RUR
10.09.2026 C222 Перевод через Систему быстрых платежей на +7999.
Без НДС.
-80,00 RUR
06.09.2026 C333 Перевод денежных средств -50,00 RUR
01.09.2026 OP1ED ЗАРПЛАТА ЗА ПЕРВУЮ ПОЛОВИНУ МЕСЯЦА.
1 234,00 RUR
HOLD Неподтвержденная операция: дата операции: 12.09.2026
-10,00 RUR
"""


SBER = """
Выписка по платёжному счёту
ПАО Сбербанк
За период 01.09.2026 — 12.09.2026
Пополнение +1 644,00
Списание 130,00
Расшифровка операций
05.09.2026 12:00 Перевод СБП +410,00 1 000,00
05.09.2026 111111 Перевод через СБП. Операция по счету
****1234
10.09.2026 16:49 Отдых и развлечения 80,00 920,00
10.09.2026 222222 Оплата. Операция по карте
****1234
01.09.2026 09:00 Перевод на карту +1 234,00 2 234,00
01.09.2026 333333 Зачисление. Операция по счету
****1234
06.09.2026 18:46 Прочие операции 50,00 2 184,00
Продолжение на следующей странице
Выписка по платёжному счёту Страница 2
"""


SBER_SPRAVKA_TEXT = """
Сформировано в Сбербанк Онлайн 11 сентября 2026 года
Справка по операции
ПАО «Сбербанк» сообщает, что указанная ниже операция зачисления была
совершена по счёту карты.
Статус операции
Перевод выполнен
"""


TBANK = """
АКЦИОНЕРНОЕ ОБЩЕСТВО «ТБАНК»
Справка о движении средств
13.09.2026
Движение средств за период с 01.09.2026 по 12.09.2026
Дата и время
операции
Дата
списания
05.09.2026
12:00
05.09.2026
12:00
+410.00 ₽ +410.00 ₽ Пополнение. Система
быстрых платежей
—
10.09.2026
16:49
10.09.2026
16:49
-80.00 ₽ -80.00 ₽ Оплата
####
01.09.2026
09:00
01.09.2026
09:00
+1 234.00 ₽ +1 234.00 ₽ Перевод средств из
Кубышки
—
Пополнения: 1 644,00 ₽
Расходы: 80,00 ₽
"""


def test_parse_statement_text_splits_credits_and_debits() -> None:
    parsed = parse_statement_text(SAMPLE)
    assert parsed is not None
    assert parsed.period_start == datetime(2026, 9, 1, 0, 0, 0)
    assert parsed.period_end == datetime(2026, 9, 12, 23, 59, 59)
    assert len(parsed.credits) == 1
    assert parsed.credits[0].amount_kopecks == 41000
    assert parsed.credits[0].occurred_at == datetime(2026, 9, 5, 12, 0, 0)
    assert len(parsed.debits) == 2
    assert {d.amount_kopecks for d in parsed.debits} == {8000, 2000}


def test_parse_account_statement_without_times() -> None:
    parsed = parse_statement_text(ACCOUNT)
    assert parsed is not None
    assert parsed.kind == "vtb_account"
    assert parsed.period_start == datetime(2026, 8, 12, 0, 0, 0)
    assert parsed.period_end == datetime(2026, 9, 12, 23, 59, 59)
    assert len(parsed.credits) == 3
    assert {c.amount_kopecks for c in parsed.credits} == {41000, 5000, 250000}
    assert parsed.credits[0].occurred_at == datetime(2026, 9, 5, 0, 0, 0)
    assert len(parsed.debits) == 1
    assert parsed.debits[0].amount_kopecks == 8000


def test_import_pdf_coverage_even_without_credits(tmp_path: Path) -> None:
    only_debits = """
Период выписки 01.09.2026 - 03.09.2026
Баланс на начало периода 1.00 RUB
Операции по карте
01.09.2026
10:00:00 01.09.2026 0.0 RUB -50.0 0.0 RUB Оплата.
"""
    parsed = parse_statement_text(only_debits)
    assert parsed is not None
    assert parsed.credits == []
    ledger = Ledger(tmp_path / "l.db")
    assert parsed.period_start and parsed.period_end
    ledger.add_coverage(parsed.period_start, parsed.period_end, source="test")
    miss = ledger.match(
        ExtractedFields(amount_kopecks=41000, occurred_at=datetime(2026, 9, 2, 13, 0)),
        "x",
    )
    assert miss.covered
    assert not miss.confirmed


@pytest.mark.skipif(not VTB_CARD, reason="нет выписки ВТБ по карте в корпусе")
def test_real_vtb_card_statement_has_period_and_only_debits() -> None:
    parsed = parse_statement(VTB_CARD[0])
    assert parsed is not None
    assert parsed.period_start is not None
    assert parsed.period_end is not None
    assert parsed.period_end > parsed.period_start
    assert parsed.debits
    assert parsed.credits == []


@pytest.mark.skipif(not VTB_CARD, reason="нет выписки ВТБ по карте в корпусе")
def test_import_real_vtb_card_statement_is_refused(tmp_path: Path) -> None:
    result = Ledger(tmp_path / "l.db").import_pdf(VTB_CARD[0])
    assert result.is_statement
    assert result.count == 0
    assert result.coverage is None
    assert result.warning is not None
    assert "по карте" in result.warning.lower() or "счёту" in result.warning.lower()


@pytest.mark.skipif(not VTB_ACCOUNT, reason="нет выписки ВТБ по счёту в корпусе")
def test_real_vtb_account_statement_has_credits_and_debits() -> None:
    parsed = parse_statement(VTB_ACCOUNT[0])
    assert parsed is not None
    assert parsed.kind == "vtb_account"
    assert parsed.period_start is not None
    assert parsed.period_end is not None
    assert parsed.credits
    assert parsed.debits


@pytest.mark.skipif(not VTB_ACCOUNT, reason="нет выписки ВТБ по счёту в корпусе")
def test_import_real_vtb_account_statement(tmp_path: Path) -> None:
    result = Ledger(tmp_path / "l.db").import_pdf(VTB_ACCOUNT[0])
    assert result.is_statement
    assert result.count >= 1
    assert result.debit_count >= 1
    assert result.coverage is not None


def test_parse_alfa_statement_splits_credits_and_skips_hold() -> None:
    parsed = parse_statement_text(ALFA)
    assert parsed is not None
    assert parsed.kind == "alfa_account"
    assert parsed.period_start == datetime(2026, 9, 1, 0, 0, 0)
    assert parsed.period_end == datetime(2026, 9, 12, 23, 59, 59)
    assert {c.amount_kopecks for c in parsed.credits} == {41000, 123400}
    assert {d.amount_kopecks for d in parsed.debits} == {8000, 5000}
    assert all(
        op.occurred_at != datetime(2026, 9, 12, 0, 0, 0)
        for op in parsed.credits + parsed.debits
    )


@pytest.mark.skipif(not ALFA_STMT, reason="нет выписки Альфы в корпусе")
def test_real_alfa_statement_has_credits_and_period() -> None:
    parsed = parse_statement(ALFA_STMT[0])
    assert parsed is not None
    assert parsed.kind == "alfa_account"
    assert parsed.period_start is not None
    assert parsed.period_end is not None
    assert parsed.period_end > parsed.period_start
    assert parsed.credits
    assert parsed.debits


@pytest.mark.skipif(not ALFA_STMT, reason="нет выписки Альфы в корпусе")
def test_import_real_alfa_statement(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_pdf(ALFA_STMT[0])
    assert result.is_statement
    assert result.count >= 1
    assert result.debit_count >= 1
    assert result.coverage is not None
    first = result.fields[0]
    assert first.amount_kopecks is not None and first.occurred_at is not None
    assert ledger.match(first, "alfa-doc").confirmed


def test_parse_sber_statement_plus_is_credit_unsigned_is_debit() -> None:
    parsed = parse_statement_text(SBER)
    assert parsed is not None
    assert parsed.kind == "sber_account"
    assert parsed.period_start == datetime(2026, 9, 1, 0, 0, 0)
    assert parsed.period_end == datetime(2026, 9, 12, 23, 59, 59)
    assert {c.amount_kopecks for c in parsed.credits} == {41000, 123400}
    assert parsed.credits[0].occurred_at == datetime(2026, 9, 5, 12, 0)
    assert {d.amount_kopecks for d in parsed.debits} == {8000, 5000}


def test_sber_one_operation_spravka_is_not_a_period_statement() -> None:
    assert not looks_like_statement(SBER_SPRAVKA_TEXT)
    assert parse_statement_text(SBER_SPRAVKA_TEXT) is None


@pytest.mark.skipif(not SBER_STMT, reason="нет выписки Сбера в корпусе")
def test_real_sber_statement_has_credits_debits_and_period() -> None:
    parsed = parse_statement(SBER_STMT[0])
    assert parsed is not None
    assert parsed.kind == "sber_account"
    assert parsed.period_start is not None
    assert parsed.period_end is not None
    assert parsed.period_end > parsed.period_start
    assert parsed.credits
    assert parsed.debits
    assert len(parsed.credits) + len(parsed.debits) >= 10


@pytest.mark.skipif(not SBER_STMT, reason="нет выписки Сбера в корпусе")
def test_import_real_sber_statement(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_pdf(SBER_STMT[0])
    parsed = parse_statement(SBER_STMT[0])
    assert parsed is not None
    assert result.is_statement
    assert result.count == len(parsed.credits)
    assert result.skipped == 0
    assert result.debit_count == len(parsed.debits)
    assert result.coverage is not None
    assert len(ledger.list_entries()) == len(parsed.credits)
    first = result.fields[0]
    assert first.amount_kopecks is not None and first.occurred_at is not None
    assert ledger.match(first, "sber-doc").confirmed


@pytest.mark.skipif(not SBER_SPRAVKA, reason="нет справки Сбера в корпусе")
def test_real_sber_spravka_is_not_imported_as_statement() -> None:
    for pdf in SBER_SPRAVKA:
        assert parse_statement(pdf) is None


def test_parse_tbank_statement_plus_is_credit() -> None:
    parsed = parse_statement_text(TBANK)
    assert parsed is not None
    assert parsed.kind == "tbank_account"
    assert parsed.period_start == datetime(2026, 9, 1, 0, 0, 0)
    assert parsed.period_end == datetime(2026, 9, 12, 23, 59, 59)
    assert {c.amount_kopecks for c in parsed.credits} == {41000, 123400}
    assert parsed.credits[0].occurred_at == datetime(2026, 9, 5, 12, 0)
    assert {d.amount_kopecks for d in parsed.debits} == {8000}


@pytest.mark.skipif(not TBANK_STMT, reason="нет выписки Т-Банка в корпусе")
def test_real_tbank_statement_has_credits_debits_and_period() -> None:
    parsed = parse_statement(TBANK_STMT[0])
    assert parsed is not None
    assert parsed.kind == "tbank_account"
    assert parsed.period_start is not None
    assert parsed.period_end is not None
    assert parsed.period_end > parsed.period_start
    assert parsed.credits
    assert parsed.debits
    assert len(parsed.credits) + len(parsed.debits) >= 10


@pytest.mark.skipif(not TBANK_STMT, reason="нет выписки Т-Банка в корпусе")
def test_import_real_tbank_statement(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    result = ledger.import_pdf(TBANK_STMT[0])
    parsed = parse_statement(TBANK_STMT[0])
    assert parsed is not None
    assert result.is_statement
    assert result.count == len(parsed.credits)
    assert result.skipped == 0
    assert result.debit_count == len(parsed.debits)
    assert result.coverage is not None
    assert len(ledger.list_entries()) == len(parsed.credits)
    first = result.fields[0]
    assert first.amount_kopecks is not None and first.occurred_at is not None
    assert ledger.match(first, "tbank-doc").confirmed


@pytest.mark.skipif(not TBANK_SPRAVKA, reason="нет справки Т-Банка в корпусе")
def test_tbank_one_operation_spravka_is_not_a_period_statement() -> None:
    for pdf in TBANK_SPRAVKA:
        assert parse_statement(pdf) is None
