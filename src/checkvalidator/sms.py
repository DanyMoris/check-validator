"""Разбор SMS Сбера о движении по счёту.

В SMS нет даты — только время. Дату берём из момента получения (когда текст
вставили в бота). У ВТБ и Альфы SMS об операциях часто платные и выключены;
образцов нет, разбирать нечего.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .extract import ExtractedFields, parse_amount

_HEAD = re.compile(
    r"^СЧ[ЁЕ]Т\d{4}\s+(\d{1,2}:\d{2})\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_CREDIT_AMT = re.compile(
    r"\+(\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:р|₽)",
    re.IGNORECASE,
)
_DEBIT_AMT = re.compile(
    r"(?<!\+)\-(\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:р|₽)",
    re.IGNORECASE,
)
_PAYER = re.compile(r"\s+от\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SmsParse:
    kind: str = "unknown"
    is_credit: bool = False
    fields: ExtractedFields | None = None

    @property
    def found(self) -> bool:
        return self.kind != "unknown"


def looks_like_sber_sms(text: str) -> bool:
    return bool(_HEAD.match(_one_line(text)))


def parse_sber_sms(text: str, *, received_at: datetime | None = None) -> SmsParse:
    line = _one_line(text)
    head = _HEAD.match(line)
    if not head:
        return SmsParse()
    when = _combine_sms_time(head.group(1), received_at or datetime.now())
    rest = head.group(2)
    credit = _CREDIT_AMT.search(rest)
    if credit:
        kopecks = parse_amount(credit.group(1) + "р")
        if not kopecks:
            return SmsParse(kind="sber")
        payer = None
        named = _PAYER.search(rest)
        if named:
            payer = named.group(1).strip()[:80]
        return SmsParse(
            kind="sber",
            is_credit=True,
            fields=ExtractedFields(
                amount_kopecks=kopecks,
                occurred_at=when,
                payer=payer,
                source="sms",
            ),
        )
    if _DEBIT_AMT.search(rest):
        return SmsParse(kind="sber", is_credit=False)
    return SmsParse(kind="sber")


def _one_line(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split())


def _combine_sms_time(hhmm: str, received_at: datetime) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    occurred = received_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if occurred > received_at + timedelta(minutes=5):
        occurred -= timedelta(days=1)
    return occurred
