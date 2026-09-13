"""Разбор текстового слоя PDF: сумма, дата, реквизиты, идентификатор операции.

Банки верстают чеки по-разному: где-то подпись поля на одной строке, значение
на следующей, где-то всё в одну строку. Ищем сначала по известным подписям,
и только если их нет — берём первую дату в шапке документа.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}

# Подписи суммы — от более частных к более общим, чтобы «Сумма в валюте
# операции» не проигрывала короткому «Сумма».
AMOUNT_LABELS = (
    "сумма перевода",
    "сумма операции",
    "сумма платежа",
    "сумма в валюте операции",
    "сколько",
    "сумма",
)
TOTAL_LABELS = ("итого",)
COMMISSION_LABELS = ("комиссия",)
DATE_LABELS = (
    "дата и время перевода",
    "дата совершения операции",
    "дата операции",
    "операция совершена",
    "чек по операции",
)
OP_ID_LABELS = (
    "идентификатор операции в сбп",
    "id операции в сбп",
    "идентификатор платежа (суип)",
    "номер операции в сбп",
    "номер операции",
    "код авторизации",
    "номер документа",
    "rrn (reference retrieval number)",
    "межбанковский код",
    "квитанция №",
    "квитанция no",
)
PAYEE_LABELS = ("получатель",)
PAYER_LABELS = ("отправитель", "плательщик")
INN_LABELS = ("инн",)
KPP_LABELS = ("кпп",)
BIK_LABELS = ("бик",)
ACCOUNT_LABELS = ("расчетный счет",)
CORR_LABELS = ("корр. счет", "корреспондентский счет")

_SKIP_AMOUNT_LINE = ("счета карты", "баланс")
_FOOTNOTES = str.maketrans("", "", "¹²³⁴⁵⁶⁷⁸⁹⁰")
_CURRENCY_RE = re.compile(
    r"(?:RUR|RUB|₽|р(?:уб(?:лей|ля|ль)?)?|\bi\b)",
    re.IGNORECASE,
)
_AMOUNT_TOKEN = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?(?!\d)"
)
_DT_NUMERIC = re.compile(
    r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?:[,\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
_DT_WORD = re.compile(
    r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s*(?:в|,)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
    re.IGNORECASE,
)
_ISO_DT = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


@dataclass(slots=True)
class ExtractedFields:
    """Что удалось прочитать из документа. Пустые поля — не ошибка сами по себе."""

    amount_kopecks: int | None = None
    commission_kopecks: int | None = None
    total_kopecks: int | None = None
    occurred_at: datetime | None = None
    operation_id: str | None = None
    payer: str | None = None
    payee: str | None = None
    inn: str | None = None
    kpp: str | None = None
    bik: str | None = None
    account: str | None = None
    corr_account: str | None = None
    text_length: int = 0
    source: str = "pdf"
    notes: list[str] = field(default_factory=list)

    @property
    def usable_for_match(self) -> bool:
        return self.amount_kopecks is not None and self.occurred_at is not None


def format_rub(kopecks: int) -> str:
    sign = "-" if kopecks < 0 else ""
    kopecks = abs(kopecks)
    rubles, frac = divmod(kopecks, 100)
    grouped = f"{rubles:,}".replace(",", " ")
    return f"{sign}{grouped},{frac:02d} ₽"


def format_dt(value: datetime) -> str:
    if value.hour == 0 and value.minute == 0 and value.second == 0:
        return value.strftime("%d.%m.%Y")
    if value.second == 0:
        return value.strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y %H:%M:%S")


def parse_amount(raw: str) -> int | None:
    """«3 146,50 ₽», «410.00 RUR», «45,00» → копейки. Нечисловое — None."""
    if raw is None:
        return None
    s = _clean_spaces(raw)
    s = _CURRENCY_RE.sub("", s).strip(" .:")
    match = _AMOUNT_TOKEN.search(s)
    if not match:
        return None
    token = match.group(0).replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    token = token.replace(",", ".")
    if token.count(".") > 1:
        return None
    try:
        if "." in token:
            whole, frac = token.split(".")
            if not whole:
                whole = "0"
            return int(whole) * 100 + int((frac + "00")[:2])
        return int(token) * 100
    except ValueError:
        return None


def parse_signed_amount(raw: str) -> int | None:
    """Как parse_amount, но сохраняет минус: «-410,00» → -41000."""
    token = (raw or "").strip().replace("−", "-").replace("–", "-")
    negative = token.startswith("-")
    if token.startswith("(") and token.endswith(")"):
        negative = True
        token = token[1:-1]
    magnitude = parse_amount(token.lstrip("+-"))
    if magnitude is None:
        return None
    return -magnitude if negative else magnitude


def parse_datetime(raw: str) -> datetime | None:
    """Понимает 02.09.2026 13:02, 2026-09-02T13:02 и «18 мая 2026 в 20:11»."""
    found, _remainder = take_datetime(raw)
    return found


def take_datetime(raw: str) -> tuple[datetime | None, str]:
    """Первая дата в строке и остаток текста без неё."""
    if not raw:
        return None, ""
    text = _clean_spaces(raw)
    match = _ISO_DT.search(text)
    if match:
        y, mo, d, hh, mm, ss = match.groups()
        return _build_dt(int(y), int(mo), int(d), hh, mm, ss), (
            text[: match.start()] + text[match.end() :]
        ).strip()
    match = _DT_NUMERIC.search(text)
    if match:
        d, mo, y, hh, mm, ss = match.groups()
        year = int(y)
        if year < 100:
            year += 2000
        return _build_dt(year, int(mo), int(d), hh, mm, ss), (
            text[: match.start()] + text[match.end() :]
        ).strip()
    match = _DT_WORD.search(text)
    if match:
        d, month, y, hh, mm, ss = match.groups()
        mo = MONTHS.get(month.lower())
        if mo:
            return _build_dt(int(y), mo, int(d), hh, mm, ss), (
                text[: match.start()] + text[match.end() :]
            ).strip()
    return None, text


def extract_pdf_text(path: str | Path) -> str:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        pages = []
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            try:
                pages.append(textpage.get_text_bounded())
            finally:
                textpage.close()
                page.close()
        return "\n".join(pages)
    finally:
        document.close()


def extract(path: str | Path) -> ExtractedFields:
    """Читает PDF и вытаскивает поля платежа. При ошибке чтения поля остаются пустыми."""
    try:
        text = extract_pdf_text(path)
    except Exception as exc:  # noqa: BLE001 — любой сбой чтения = пустые поля, не падение проверки
        return ExtractedFields(notes=[f"text_read_error:{type(exc).__name__}"])
    return extract_from_text(text)


def extract_from_text(text: str) -> ExtractedFields:
    text = _clean_spaces(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    fields = ExtractedFields(text_length=len(text))

    amount_raw = _value_after(lines, AMOUNT_LABELS, skip_if=_SKIP_AMOUNT_LINE)
    fields.amount_kopecks = parse_amount(amount_raw) if amount_raw else None

    total_raw = _value_after(lines, TOTAL_LABELS)
    fields.total_kopecks = parse_amount(total_raw) if total_raw else None

    comm_raw = _value_after(lines, COMMISSION_LABELS)
    if comm_raw and "без комиссии" in _fold(comm_raw):
        fields.commission_kopecks = 0
    elif comm_raw:
        fields.commission_kopecks = parse_amount(comm_raw)

    date_raw = _value_after(lines, DATE_LABELS)
    fields.occurred_at = parse_datetime(date_raw) if date_raw else None
    if fields.occurred_at is None:
        header = "\n".join(lines[:6])
        fields.occurred_at = parse_datetime(header)

    op_raw = _value_after(lines, OP_ID_LABELS)
    if op_raw:
        fields.operation_id = _clip_id(op_raw)

    payee = _value_after(lines, PAYEE_LABELS)
    if payee and not _looks_like_amount(payee):
        fields.payee = payee[:80]
    payer = _value_after(lines, PAYER_LABELS)
    if payer and not _looks_like_amount(payer):
        fields.payer = payer[:80]

    inn_raw = _value_after(lines, INN_LABELS)
    inn_digits = _only_digits(inn_raw or "", (10, 12))
    if inn_digits:
        fields.inn = inn_digits

    kpp_raw = _value_after(lines, KPP_LABELS)
    kpp_digits = _only_digits(kpp_raw or "", (9,))
    if kpp_digits:
        fields.kpp = kpp_digits

    bik_raw = _value_after(lines, BIK_LABELS)
    bik_digits = _only_digits(bik_raw or "", (9,))
    if bik_digits:
        fields.bik = bik_digits

    acc_raw = _value_after(lines, ACCOUNT_LABELS)
    acc_digits = _only_digits(acc_raw or "", (20,))
    if not acc_digits:
        acc_digits = _account_before_label(lines)
    if acc_digits:
        fields.account = acc_digits

    corr_raw = _value_after(lines, CORR_LABELS)
    corr_digits = _only_digits(corr_raw or "", (20,))
    if corr_digits:
        fields.corr_account = corr_digits

    return fields


_INCOMING_MARKERS = (
    "входящий перевод",
    "операция зачисления",
    "пополнение",
    "счет зачисления",
    "зачисление было",
)
_OUTGOING_MARKERS = (
    "по вопросам зачисления обращайтесь к получателю",
    "счет списания",
    "сумма перевода",
    "перевод отправлен",
    "исходящий перевод",
    "списано со счета",
    "списано со счёта",
)


def document_direction(text: str) -> str:
    """incoming / outgoing / unknown — по формулировкам банка, не по папке корпуса."""
    folded = _fold(_clean_spaces(text).replace("\n", " "))
    if any(marker in folded for marker in _INCOMING_MARKERS):
        return "incoming"
    if any(marker in folded for marker in _OUTGOING_MARKERS):
        return "outgoing"
    return "unknown"


def extract_operations(path: str | Path) -> list[ExtractedFields]:
    """Операции из PDF: все зачисления из выписки либо одна операция из справки/чека."""
    from .statements import parse_statement

    parsed = parse_statement(path)
    if parsed is not None:
        return list(parsed.credits)
    fields = extract(path)
    return [fields] if fields.usable_for_match else []


def income_hint(fields: ExtractedFields) -> str | None:
    """Команда, которой это поступление можно внести в журнал вручную."""
    if not fields.usable_for_match or fields.amount_kopecks is None or fields.occurred_at is None:
        return None
    amount = format_rub(fields.amount_kopecks).replace(" ₽", "")
    return f"/income {amount} {format_dt(fields.occurred_at)}"


def _clean_spaces(text: str) -> str:
    return text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")


def _fold(line: str) -> str:
    return line.translate(_FOOTNOTES).replace("ё", "е").replace("Ё", "Е").strip(" :.").lower()


def _value_after(
    lines: list[str],
    labels: tuple[str, ...],
    skip_if: tuple[str, ...] = (),
) -> str | None:
    """Значение сразу после подписи: на той же строке или на следующей."""
    for i, raw in enumerate(lines):
        folded = _fold(raw)
        if any(skip in folded for skip in skip_if):
            continue
        for label in labels:
            if folded == label:
                return lines[i + 1].strip() if i + 1 < len(lines) else None
            if folded.startswith(label) and len(folded) > len(label):
                visible = raw.translate(_FOOTNOTES).translate(str.maketrans("Ёё", "Ее")).strip()
                parts = re.split(
                    re.compile("^" + re.escape(label), re.IGNORECASE),
                    visible,
                    maxsplit=1,
                )
                leftover = parts[1].lstrip(" :№#") if len(parts) == 2 else ""
                if leftover:
                    return leftover.strip()
                return lines[i + 1].strip() if i + 1 < len(lines) else None
    return None


def _account_before_label(lines: list[str]) -> str | None:
    """В чеке Сбера 20 цифр счёта идут перед подписью «Расчётный счёт» из-за колонок."""
    for i, raw in enumerate(lines):
        if _fold(raw) not in ACCOUNT_LABELS:
            continue
        if i == 0:
            return None
        prev = _only_digits(lines[i - 1], (20,))
        if prev:
            return prev
    return None


def _only_digits(raw: str, lengths: tuple[int, ...]) -> str | None:
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) in lengths else None


def _clip_id(raw: str) -> str:
    value = raw.strip()
    value = re.split(r"[\n\r]", value, maxsplit=1)[0].strip()
    if value.startswith("(") and ")" in value:
        after = value.split(")", 1)[1].strip()
        if after:
            value = after
    return value[:80] if value else ""


def _looks_like_amount(raw: str) -> bool:
    return parse_amount(raw) is not None and len(re.sub(r"\D", "", raw)) <= 12


def _build_dt(
    year: int, month: int, day: int, hh: str | None, mm: str | None, ss: str | None
) -> datetime | None:
    try:
        return datetime(year, month, day, int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return None
