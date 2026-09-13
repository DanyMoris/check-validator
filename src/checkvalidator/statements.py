"""Разбор выписки за период: список операций и границы покрытия.

В журнал просим только выписку по счёту (не по карте): в ней видны все
зачисления, включая СБП. ВТБ по карте разбирается, но не импортируется.
ВТБ по счёту: две даты, три суммы с RUB. Альфа-Банк: «За период с … по …»,
сумма в RUR часто на следующей строке.
Т-Банк: «Справка о движении средств», «за период с … по …»; две даты со
временем, сумма со знаком и ₽ (плюс — зачисление).
Сбер: «За период ДД.ММ.ГГГГ — ДД.ММ.ГГГГ», строка с датой и временем; плюс у
первой суммы — зачисление, без плюса — списание. Справка по одной операции
сюда не относится. В журнал идут только плюсы — зачисления. Минусы (оплаты)
нужны, чтобы не принять расход за приход. HOLD Альфа не импортируется.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .extract import (
    ExtractedFields,
    extract_pdf_text,
    parse_amount,
    parse_datetime,
    parse_signed_amount,
)

_PERIOD = re.compile(
    r"период выписки\s+(\d{1,2}\.\d{1,2}\.\d{2,4})\s*[-–—]\s*(\d{1,2}\.\d{1,2}\.\d{2,4})",
    re.IGNORECASE,
)
_ALFA_PERIOD = re.compile(
    r"за период с\s+(\d{1,2}\.\d{1,2}\.\d{2,4})\s+по\s+(\d{1,2}\.\d{1,2}\.\d{2,4})",
    re.IGNORECASE,
)
_SBER_PERIOD = re.compile(
    r"за период\s+(\d{1,2}\.\d{1,2}\.\d{2,4})\s*[-–—]\s*(\d{1,2}\.\d{1,2}\.\d{2,4})",
    re.IGNORECASE,
)
_AMT = r"[+\-−]?(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?"
_ALFA_AMT = r"[+\-−]?(?:\d{1,3}(?:[ \u00a0 ]\d{3})+|\d+)[.,]\d{2}"
_ALFA_DATE = re.compile(rf"^(\d{{2}}\.\d{{2}}\.\d{{4}})\s+(\S+)\s+(.*)$")
_ALFA_AMT_LINE = re.compile(rf"^({_ALFA_AMT})\s+RUR\.?$", re.IGNORECASE)
_ALFA_AMT_TAIL = re.compile(rf"({_ALFA_AMT})\s+RUR\.?\s*$", re.IGNORECASE)
# Только запятая: иначе «12.09.2026» читается как сумма.
_SBER_AMT = re.compile(
    r"(?P<sign>[+\-−])?\s*(?P<mag>(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+),\d{2})"
)
_SBER_HEAD = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}(?::\d{2})?)\s+(.+)$")
_TBANK_PERIOD = re.compile(
    r"движени[ея] средств за период с\s+(\d{1,2}\.\d{1,2}\.\d{2,4})\s+по\s+(\d{1,2}\.\d{1,2}\.\d{2,4})",
    re.IGNORECASE,
)
_TBANK_AMT = re.compile(
    r"(?P<sign>[+\-−])\s*(?P<mag>(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)[.,]\d{2})\s*₽"
)
_TBANK_DATE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})$")
_TBANK_TIME = re.compile(r"^(\d{2}:\d{2}(?::\d{2})?)$")
_OP_CARD = re.compile(
    r"(?P<op_date>\d{2}\.\d{2}\.\d{4})\s+"
    r"(?P<op_time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<bank_date>\d{2}\.\d{2}\.\d{4})\s+"
    r"(?P<orig>[+\-−]?\d+(?:[.,]\d+)?)\s+RUB\s+"
    r"(?P<card>[+\-−]?\d+(?:[.,]\d+)?)\s+"
    r"(?P<fee>[+\-−]?\d+(?:[.,]\d+)?)\s+RUB\s+"
    r"(?P<desc>.+?)"
    r"(?=(?:\s+\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})|\s*$)",
    re.IGNORECASE | re.DOTALL,
)
# Выписка по счёту: нет времени, три суммы с валютой. У прихода все плюсы
# (сумма, сумма в валюте счёта, комиссия); у расхода первая и третья с минусом.
_OP_ACCOUNT = re.compile(
    rf"(?P<op_date>\d{{2}}\.\d{{2}}\.\d{{4}})\s+"
    rf"(?P<bank_date>\d{{2}}\.\d{{2}}\.\d{{4}})\s+"
    rf"(?P<a1>{_AMT})\s+RUB\s+"
    rf"(?P<a2>{_AMT})\s+RUB\s+"
    rf"(?P<a3>{_AMT})\s+RUB",
    re.IGNORECASE,
)


@dataclass(slots=True)
class StatementParse:
    period_start: datetime | None = None
    period_end: datetime | None = None
    credits: list[ExtractedFields] = field(default_factory=list)
    debits: list[ExtractedFields] = field(default_factory=list)
    kind: str = "unknown"

    @property
    def found(self) -> bool:
        return self.kind != "unknown" or bool(self.credits or self.debits or self.period_start)


def looks_like_statement(text: str) -> bool:
    if _looks_like_sber(text) or _looks_like_tbank(text):
        return True
    folded = _fold_text(text)
    if "период выписки" in folded:
        return True
    if "за период с" in folded and "выписка по счету" in folded:
        return True
    if "дата проводки" in folded and "операции по счету" in folded:
        return True
    if "операции по счету" in folded and "баланс на начало" in folded:
        return True
    return "операции по карте" in folded and "баланс на начало" in folded


CARD_STATEMENT_HINT = (
    "Это выписка по карте. Нужна выписка по счёту: в ней видны все зачисления, "
    "включая переводы СБП на счёт. Выписку по карте в журнал не записываю."
)


def _looks_like_alfa(text: str) -> bool:
    folded = _fold_text(text)
    if "альфа-банк" in folded or "альфа банк" in folded:
        return "выписка" in folded
    return "за период с" in folded and "дата проводки" in folded


def _looks_like_tbank(text: str) -> bool:
    folded = _fold_text(text)
    return "справка о движении средств" in folded


def _looks_like_sber(text: str) -> bool:
    folded = _fold_text(text)
    if "справка по операции" in folded:
        return False
    if "выписка по платежному счету" in folded:
        return True
    return "расшифровка операций" in folded and "за период" in folded


def _fold_text(text: str) -> str:
    return text.replace("\u00a0", " ").lower().replace("ё", "е")


def parse_statement(path: str | Path) -> StatementParse | None:
    try:
        text = extract_pdf_text(path)
    except Exception:  # noqa: BLE001
        return None
    return parse_statement_text(text)


def parse_statement_text(text: str) -> StatementParse | None:
    if not looks_like_statement(text):
        return None
    if _looks_like_tbank(text):
        return _parse_tbank(text)
    if _looks_like_sber(text):
        return _parse_sber(text)
    if _looks_like_alfa(text):
        return _parse_alfa(text)
    return _parse_vtb(text)


def _parse_vtb(text: str) -> StatementParse:
    blob = re.sub(r"[\r\n]+", " ", text.replace("\u00a0", " "))
    parsed = StatementParse(kind="vtb_statement")
    _apply_period(parsed, _PERIOD.search(blob))

    card_hits = list(_OP_CARD.finditer(blob))
    if card_hits:
        parsed.kind = "vtb_card"
        for match in card_hits:
            occurred = parse_datetime(f"{match.group('op_date')} {match.group('op_time')}")
            _append_signed(parsed, occurred, parse_signed_amount(match.group("card")))
        return parsed

    account_hits = list(_OP_ACCOUNT.finditer(blob))
    if account_hits:
        parsed.kind = "vtb_account"
        for match in account_hits:
            occurred = parse_datetime(match.group("op_date"))
            signed = _account_movement(
                parse_signed_amount(match.group("a1")),
                parse_signed_amount(match.group("a2")),
                parse_signed_amount(match.group("a3")),
            )
            _append_signed(parsed, occurred, signed)
    return parsed


def _parse_alfa(text: str) -> StatementParse:
    """Выписка Альфа-Банка: дата и код на одной строке, сумма в RUR часто на следующей.

    Неподтверждённые HOLD в журнал не идут.
    """
    parsed = StatementParse(kind="alfa_account")
    blob = re.sub(r"[\r\n]+", " ", text.replace("\u00a0", " "))
    _apply_period(parsed, _ALFA_PERIOD.search(blob))

    pending: datetime | None = None
    for raw in text.splitlines():
        ln = raw.replace("\u00a0", " ").strip()
        if not ln:
            continue
        if ln.upper().startswith("HOLD"):
            break
        started = _ALFA_DATE.match(ln)
        if started:
            pending = parse_datetime(started.group(1))
            tail = _ALFA_AMT_TAIL.search(started.group(3))
            if tail:
                _append_signed(parsed, pending, parse_signed_amount(tail.group(1)))
                pending = None
            continue
        if pending is None:
            continue
        amount = _ALFA_AMT_LINE.match(ln)
        if amount:
            _append_signed(parsed, pending, parse_signed_amount(amount.group(1)))
            pending = None
    return parsed


def _parse_tbank(text: str) -> StatementParse:
    """Справка о движении средств Т-Банка: две даты со временем, сумма со знаком и ₽."""
    parsed = StatementParse(kind="tbank_account")
    blob = text.replace("\u00a0", " ")
    _apply_period(parsed, _TBANK_PERIOD.search(blob))
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    i = 0
    while i < len(lines) - 4:
        if not (
            _TBANK_DATE.match(lines[i])
            and _TBANK_TIME.match(lines[i + 1])
            and _TBANK_DATE.match(lines[i + 2])
            and _TBANK_TIME.match(lines[i + 3])
        ):
            i += 1
            continue
        found = list(_TBANK_AMT.finditer(lines[i + 4]))
        if not found:
            i += 1
            continue
        first = found[0]
        kopecks = parse_amount(first.group("mag"))
        if not kopecks:
            i += 5
            continue
        occurred = parse_datetime(f"{lines[i]} {lines[i + 1]}")
        signed = kopecks if first.group("sign") == "+" else -kopecks
        _append_signed(parsed, occurred, signed)
        i += 5
    return parsed


def _parse_sber(text: str) -> StatementParse:
    """Выписка Сбера: дата и время, затем категория и суммы. Плюс только у прихода."""
    parsed = StatementParse(kind="sber_account")
    blob = text.replace("\u00a0", " ")
    _apply_period(parsed, _SBER_PERIOD.search(blob))

    for raw in blob.splitlines():
        ln = raw.strip()
        head = _SBER_HEAD.match(ln)
        if not head:
            continue
        rest = head.group(3)
        folded_rest = rest.lower()
        if "страница" in folded_rest or "продолжение" in folded_rest:
            continue
        found = list(_SBER_AMT.finditer(rest))
        if not found:
            continue
        first = found[0]
        kopecks = parse_amount(first.group("mag"))
        if not kopecks:
            continue
        occurred = parse_datetime(f"{head.group(1)} {head.group(2)}")
        sign = first.group("sign")
        signed = kopecks if sign == "+" else -kopecks
        _append_signed(parsed, occurred, signed)
    return parsed


def _apply_period(parsed: StatementParse, match: re.Match[str] | None) -> None:
    if match is None:
        return
    start = parse_datetime(match.group(1))
    end = parse_datetime(match.group(2))
    if start is not None:
        parsed.period_start = start.replace(hour=0, minute=0, second=0)
    if end is not None:
        parsed.period_end = end.replace(hour=23, minute=59, second=59)


def _append_signed(
    parsed: StatementParse, occurred: datetime | None, signed: int | None
) -> None:
    if occurred is None or signed is None or signed == 0:
        return
    fields = ExtractedFields(
        amount_kopecks=abs(signed),
        occurred_at=occurred,
        operation_id=None,
        source="statement",
    )
    if signed > 0:
        parsed.credits.append(fields)
    else:
        parsed.debits.append(fields)


def _account_movement(a1: int | None, a2: int | None, a3: int | None) -> int | None:
    """Знак и сумма движения по счёту из трёх колонок PDF."""
    vals = [v for v in (a1, a2, a3) if v is not None]
    if not vals:
        return None
    negatives = [v for v in vals if v < 0]
    if negatives:
        return min(negatives)
    # Приход: сумма и сумма в валюте счёта, третья колонка — комиссия.
    principals = [v for v in (a1, a2) if v is not None and v > 0]
    if principals:
        return max(principals)
    return max(vals)
