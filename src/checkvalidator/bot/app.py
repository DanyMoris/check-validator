"""Запуск Telegram-бота.

Пользователь присылает PDF, выбирает банк и тип документа (или пропускает выбор),
получает вердикт. Скриншоты отклоняются. Доступ ограничивается списком ID.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from time import monotonic

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from checkvalidator.engine import analyse
from checkvalidator.extract import extract_pdf_text, format_dt, format_rub
from checkvalidator.ledger import Ledger, LedgerImport, parse_coverage_args, parse_income_args
from checkvalidator.profiles import ProfileRegistry
from checkvalidator.sms import looks_like_sber_sms
from checkvalidator.statements import looks_like_statement

from .access import Allowlist, RateLimiter
from .config import Settings
from .formatters import (
    format_coverage_help,
    format_denied,
    format_help,
    format_income_help,
    format_ledger,
    format_report,
    format_reset_help,
    format_statement_help,
    format_wait,
    format_withdraw_help,
    is_pdf_bytes,
    split_html_messages,
)
from .storage import Store

log = logging.getLogger("checkvalidator.bot")

BANKS = (
    ("VTB", "ВТБ"),
    ("SBER", "Сбер"),
    ("ALFA", "Альфа"),
    ("TBANK", "Т-Банк"),
)
DOC_TYPES = ("Чек", "Квитанция", "Справка")
PENDING_TTL_SECONDS = 15 * 60

router = Router()


@dataclass(slots=True)
class PendingFile:
    path: Path
    sha256: str
    filename: str
    user_id: int
    saved_at: float


@dataclass(slots=True)
class Runtime:
    settings: Settings
    allowlist: Allowlist
    limiter: RateLimiter
    store: Store
    ledger: Ledger
    registry: ProfileRegistry
    pending: dict[int, PendingFile]
    awaiting_income: dict[int, float]
    awaiting_statement: dict[int, float]
    awaiting_withdraw: dict[int, float]


def bank_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=f"b:{code}")
            for code, label in BANKS[:2]
        ],
        [
            InlineKeyboardButton(text=label, callback_data=f"b:{code}")
            for code, label in BANKS[2:]
        ],
        [InlineKeyboardButton(text="Определить самим", callback_data="b:auto")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def type_keyboard(bank: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=doc_type, callback_data=f"t:{bank}/{doc_type}")]
        for doc_type in DOC_TYPES
    ]
    rows.append([InlineKeyboardButton(text="Не знаю тип", callback_data="t:auto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _import_kind(item: LedgerImport) -> str:
    if item.source == "csv":
        return "CSV"
    if item.source == "statement-pdf":
        return "PDF"
    return item.source


def _import_button_text(item: LedgerImport) -> str:
    name = item.label
    if len(name) > 28:
        name = name[:25] + "…"
    return f"{_import_kind(item)} · {name} · {item.entry_count} зачисл."


def reset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, очистить журнал", callback_data="rs:ok"),
                InlineKeyboardButton(text="Отмена", callback_data="rs:no"),
            ]
        ]
    )


def withdraw_list_keyboard(items: list[LedgerImport]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_import_button_text(item), callback_data=f"wd:{item.id}")]
        for item in items
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="wd:no")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def withdraw_confirm_keyboard(import_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, убрать эту загрузку", callback_data=f"wdok:{import_id}"),
                InlineKeyboardButton(text="Отмена", callback_data="wd:no"),
            ]
        ]
    )


def _withdraw_confirm_text(item: LedgerImport) -> str:
    extra = ""
    if item.consumed_count:
        extra = (
            f" Из них уже подтверждали чеки: {item.consumed_count} — "
            "повторная проверка этих файлов снова потребует поступления в журнале."
        )
    cover = (
        f", окон покрытия: {item.coverage_count}" if item.coverage_count else ""
    )
    return (
        f"Убрать {escape(_import_kind(item))} «{escape(item.label)}»?\n"
        f"Поступлений в этой загрузке: {item.entry_count}{cover}.{extra}"
    )


async def _deny(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await message.answer(format_denied(user_id))


def _is_allowed(message: Message, runtime: Runtime) -> bool:
    user = message.from_user
    return bool(user and runtime.allowlist.permits(user.id))


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else 0


def _still_waiting(bucket: dict[int, float], user_id: int) -> bool:
    started = bucket.get(user_id)
    if started is None:
        return False
    if monotonic() - started > PENDING_TTL_SECONDS:
        bucket.pop(user_id, None)
        return False
    return True


def _feed_kind(runtime: Runtime, user_id: int) -> str | None:
    if _still_waiting(runtime.awaiting_statement, user_id):
        return "statement"
    if _still_waiting(runtime.awaiting_income, user_id):
        return "income"
    return None


async def _ingest_income_file(
    message: Message,
    runtime: Runtime,
    *,
    data: bytes,
    name: str,
    looks_csv: bool,
    statement_session: bool = False,
) -> None:
    user_id = _user_id(message)
    dest_dir = runtime.settings.inbox_dir / str(user_id) / "income"
    dest_dir.mkdir(parents=True, exist_ok=True)

    def close_session() -> None:
        runtime.awaiting_income.pop(user_id, None)
        runtime.awaiting_statement.pop(user_id, None)

    if looks_csv or not is_pdf_bytes(data):
        result = runtime.ledger.import_csv(data, filename=name)
        failed = not result.count and not result.skipped and result.coverage is None
        if failed:
            if not statement_session:
                close_session()
            await message.answer(
                "В файле не нашлось строк с датой и суммой. "
                "Сделайте CSV с колонками дата и сумма, либо "
                "<code>/income 410 02.09.2026 13:02</code>."
            )
            return
        close_session()
        parts = [f"Добавил поступлений: {result.count}."]
        if result.skipped:
            parts.append(f"Уже были в журнале: {result.skipped}.")
        if result.debit_count:
            parts.append(f"Расходных операций пропущено: {result.debit_count}.")
        if result.coverage:
            parts.append(
                f"Период {format_dt(result.coverage.start_at)} – "
                f"{format_dt(result.coverage.end_at)} отмечен как полная выписка."
            )
        elif result.count + result.skipped < 2:
            parts.append("Покрытие периода не создано (нужно хотя бы две строки).")
        await message.answer(" ".join(parts))
        return

    dest = dest_dir / f"{hashlib.sha256(data).hexdigest()}.pdf"
    dest.write_bytes(data)
    result = runtime.ledger.import_pdf(dest, filename=name)
    if result.direction == "outgoing" and not result.is_statement:
        if not statement_session:
            close_session()
        await message.answer(escape(result.warning or "Исходящий перевод в журнал не записываю."))
        return
    if (
        result.is_statement
        and result.count == 0
        and result.coverage is None
        and result.warning
    ):
        if not statement_session:
            close_session()
        await message.answer(escape(result.warning))
        return
    failed = result.count == 0 and not result.is_statement and not result.skipped
    if failed:
        if not statement_session:
            close_session()
        await message.answer(
            "Не прочитал сумму и дату из этого PDF. "
            "Пришлите CSV или введите <code>/income 410 02.09.2026 13:02</code> вручную."
        )
        return
    close_session()
    if result.is_statement:
        lines = [
            f"Выписка разобрана: зачислений {result.count}, "
            f"расходных операций пропущено {result.debit_count}."
        ]
    else:
        lines = [
            f"Записал поступлений из PDF: {result.count}."
            + (" Покрытие периода не создано." if result.coverage is None else "")
        ]
    for fields in result.fields:
        if fields.amount_kopecks is not None and fields.occurred_at is not None:
            lines.append(f"• {format_rub(fields.amount_kopecks)} · {format_dt(fields.occurred_at)}")
    if result.skipped:
        lines.append(f"Уже были в журнале (не дублировал): {result.skipped}.")
    if result.coverage:
        lines.append(
            f"Период {format_dt(result.coverage.start_at)} – "
            f"{format_dt(result.coverage.end_at)} отмечен как полная выписка."
        )
    if result.warning:
        lines.append(escape(result.warning))
    for chunk in split_html_messages(lines):
        await message.answer(chunk)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    await message.answer(format_help(runtime.allowlist.is_open))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    await message.answer(f"Ваш Telegram ID: <code>{user_id}</code>")


@router.message(Command("profiles"))
async def cmd_profiles(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    if not len(runtime.registry):
        await message.answer(
            "Эталонов пока нет. Соберите их командой:\n"
            "<code>python tools/build_profiles.py</code>"
        )
        return
    lines = ["<b>Собранные эталоны</b>"]
    for profile in sorted(runtime.registry, key=lambda p: p.id):
        mark = "" if profile.established else " — мало образцов, проверка мягче"
        lines.append(f"• <code>{profile.id}</code>{mark}")
    await message.answer("\n".join(lines))


@router.message(Command("income"))
async def cmd_income(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    parsed = parse_income_args(message.text or "")
    if parsed is None:
        runtime.awaiting_income[_user_id(message)] = monotonic()
        await message.answer(format_income_help())
        return
    amount, occurred, comment = parsed
    runtime.ledger.add_entry(amount, occurred, description=comment, source="manual")
    runtime.awaiting_income.pop(_user_id(message), None)
    extra = f" ({escape(comment)})" if comment else ""
    await message.answer(
        f"Записал поступление {format_rub(amount)} за {format_dt(occurred)}{extra}. "
        "Покрытие периода не создано — отсутствие такого прихода само по себе "
        "ещё не подделка."
    )


@router.message(Command("statement"))
async def cmd_statement(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    runtime.awaiting_statement[_user_id(message)] = monotonic()
    await message.answer(format_statement_help())


@router.message(Command("coverage"))
async def cmd_coverage(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    parsed = parse_coverage_args(message.text or "")
    if parsed is None:
        await message.answer(format_coverage_help())
        return
    start, end = parsed
    runtime.ledger.add_coverage(start, end, source="manual")
    await message.answer(
        f"Период {format_dt(start)} – {format_dt(end)} отмечен как полная выписка. "
        "Чек с суммой и датой внутри окна, которого нет в журнале, будет "
        "«НЕ ПОДТВЕРЖДЁН», а не подделка."
    )


@router.message(Command("ledger"))
async def cmd_ledger(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    texts = format_ledger(runtime.ledger.list_entries(), runtime.ledger.list_coverage())
    for text in texts:
        await message.answer(text)


@router.message(Command("ledger_reset"))
async def cmd_ledger_reset(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    entries, coverage = runtime.ledger.counts()
    if entries == 0 and coverage == 0:
        await message.answer("Журнал уже пуст.")
        return
    await message.answer(
        format_reset_help(entries, coverage),
        reply_markup=reset_keyboard(),
    )


@router.message(Command("withdraw"))
async def cmd_withdraw(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    runtime.awaiting_withdraw[_user_id(message)] = monotonic()
    items = runtime.ledger.list_imports(8)
    if not items:
        runtime.awaiting_withdraw.pop(_user_id(message), None)
        await message.answer(
            "Нет загруженных CSV или PDF в журнале. "
            "Ручные /income так не убрать — для полной очистки /ledger_reset."
        )
        return
    await message.answer(format_withdraw_help(), reply_markup=withdraw_list_keyboard(items))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    user_id = _user_id(message)
    runtime.pending.pop(user_id, None)
    runtime.awaiting_income.pop(user_id, None)
    runtime.awaiting_statement.pop(user_id, None)
    runtime.awaiting_withdraw.pop(user_id, None)
    await message.answer("Отменил. Пришлите PDF чека или /income, чтобы добавить поступление.")


@router.message(F.photo)
async def reject_photo(message: Message, runtime: Runtime) -> None:
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    await message.answer(
        "Скриншоты не проверяю. Выгрузите PDF из приложения банка и пришлите его "
        "как файл (скрепка → Файл), не как фото."
    )


@router.message(F.document)
async def on_document(message: Message, bot: Bot, runtime: Runtime) -> None:
    user = message.from_user
    if user is None or not runtime.allowlist.permits(user.id):
        await _deny(message)
        return

    wait = runtime.limiter.check(user.id)
    if wait > 0:
        await message.answer(format_wait(wait))
        return

    document = message.document
    assert document is not None
    name = document.file_name or "document.pdf"
    mime = (document.mime_type or "").lower()
    looks_csv = name.lower().endswith(".csv") or mime in {
        "text/csv",
        "application/vnd.ms-excel",
    }
    looks_pdf = name.lower().endswith(".pdf") or mime == "application/pdf"
    if not looks_pdf and not looks_csv:
        await message.answer("Нужен файл PDF (чек) или CSV (выписка). Скриншоты, Word и архивы не принимаются.")
        return
    if document.file_size and document.file_size > runtime.settings.max_file_bytes:
        await message.answer(
            f"Файл слишком большой (больше {runtime.settings.max_file_mb:g} МБ)."
        )
        return

    buffer = BytesIO()
    await bot.download(document, destination=buffer)
    data = buffer.getvalue()

    digest = hashlib.sha256(data).hexdigest()
    if _still_waiting(runtime.awaiting_withdraw, user.id):
        await _offer_withdraw_by_file(message, runtime, digest=digest, name=name)
        return
    kind = _feed_kind(runtime, user.id)
    if looks_csv or kind is not None:
        await _ingest_income_file(
            message,
            runtime,
            data=data,
            name=name,
            looks_csv=looks_csv,
            statement_session=kind == "statement",
        )
        return

    if not is_pdf_bytes(data):
        await message.answer(
            "Файл называется PDF, но внутри это не PDF. Такой документ не проверяю."
        )
        return

    dest = runtime.settings.inbox_dir / str(user.id) / f"{digest}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    try:
        as_statement = looks_like_statement(extract_pdf_text(dest))
    except Exception:
        as_statement = False
    if as_statement:
        await _ingest_income_file(
            message,
            runtime,
            data=data,
            name=name,
            looks_csv=False,
            statement_session=False,
        )
        return

    runtime.pending[user.id] = PendingFile(
        path=dest,
        sha256=digest,
        filename=name,
        user_id=user.id,
        saved_at=monotonic(),
    )
    await message.answer(
        "Файл получен. Укажите банк — сверка с эталоном будет точнее. "
        "Если не уверены, нажмите «Определить самим».",
        reply_markup=bank_keyboard(),
    )


@router.message(F.text)
async def fallback_text(message: Message, runtime: Runtime) -> None:
    if message.text and message.text.startswith("/"):
        return
    if not _is_allowed(message, runtime):
        await _deny(message)
        return
    user_id = _user_id(message)
    if await _ingest_sms(message, runtime, message.text or ""):
        return
    if _feed_kind(runtime, user_id) is not None:
        parsed = parse_income_args(message.text or "")
        if parsed is not None:
            amount, occurred, comment = parsed
            runtime.ledger.add_entry(amount, occurred, description=comment, source="manual")
            if _feed_kind(runtime, user_id) != "statement":
                runtime.awaiting_income.pop(user_id, None)
            extra = f" ({escape(comment)})" if comment else ""
            await message.answer(
                f"Записал поступление {format_rub(amount)} за {format_dt(occurred)}{extra}."
            )
            return
        await message.answer(
            "Жду сумму и дату, CSV или PDF входящей справки. Пример: "
            "<code>410 02.09.2026 13:02</code>. /cancel — отмена."
        )
        return
    await message.answer(
        "Пришлите PDF-файл чека, квитанции или справки. /help — как это работает."
    )


async def _ingest_sms(message: Message, runtime: Runtime, text: str) -> bool:
    if not looks_like_sber_sms(text):
        return False
    result = runtime.ledger.import_sms(text, received_at=datetime.now())
    user_id = _user_id(message)
    if _feed_kind(runtime, user_id) == "income":
        runtime.awaiting_income.pop(user_id, None)
    if result.warning:
        await message.answer(escape(result.warning))
        return True
    if result.skipped:
        await message.answer("Такое поступление уже есть в журнале — не дублировал.")
        return True
    fields = result.fields
    if fields is None or fields.amount_kopecks is None or fields.occurred_at is None:
        await message.answer("Не прочитал сумму и время из SMS.")
        return True
    await message.answer(
        f"Записал поступление {format_rub(fields.amount_kopecks)} за "
        f"{format_dt(fields.occurred_at)} (SMS Сбера). "
        "Покрытие периода не создано."
    )
    return True


def _take_pending(runtime: Runtime, user_id: int) -> PendingFile | None:
    pending = runtime.pending.get(user_id)
    if pending is None:
        return None
    if monotonic() - pending.saved_at > PENDING_TTL_SECONDS:
        runtime.pending.pop(user_id, None)
        return None
    return pending


async def _finish_check(
    callback: CallbackQuery, runtime: Runtime, expected: str | None
) -> None:
    user = callback.from_user
    if user is None or not runtime.allowlist.permits(user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    pending = _take_pending(runtime, user.id)
    if pending is None:
        await callback.answer("Файл устарел — пришлите PDF ещё раз", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        hint = "Определяю шаблон сами…" if not expected else f"Сверяю с {expected}…"
        await callback.message.edit_text(hint)

    previous = runtime.store.last_by_hash(pending.sha256)
    loop = asyncio.get_running_loop()
    try:
        report = await loop.run_in_executor(
            None,
            lambda: analyse(
                pending.path,
                runtime.registry,
                expected=expected,
                ledger=runtime.ledger,
                consume=True,
                doc_hash=pending.sha256,
            ),
        )
    except Exception:
        log.exception("Анализ файла не удался")
        if callback.message:
            await callback.message.edit_text(
                "Не получилось разобрать файл. Пришлите его ещё раз. "
                "Если ошибка повторится — файл, скорее всего, повреждён."
            )
        return

    runtime.store.record(
        sha256=pending.sha256,
        user_id=user.id,
        filename=pending.filename,
        expected=expected,
        profile_id=report.profile_id,
        verdict=report.verdict.value,
    )
    runtime.pending.pop(user.id, None)

    text = format_report(report, previous=previous)
    if callback.message:
        await callback.message.edit_text(text)


@router.callback_query(F.data.startswith("b:"))
async def on_bank(callback: CallbackQuery, runtime: Runtime) -> None:
    user = callback.from_user
    if user is None or not runtime.allowlist.permits(user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if _take_pending(runtime, user.id) is None:
        await callback.answer("Файл устарел — пришлите PDF ещё раз", show_alert=True)
        return

    choice = (callback.data or "b:auto")[2:]
    if choice == "auto":
        await _finish_check(callback, runtime, expected=None)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Банк: {choice}. Теперь выберите тип документа.",
            reply_markup=type_keyboard(choice),
        )


@router.callback_query(F.data.startswith("t:"))
async def on_type(callback: CallbackQuery, runtime: Runtime) -> None:
    choice = (callback.data or "t:auto")[2:]
    expected = None if choice == "auto" else choice
    await _finish_check(callback, runtime, expected)


@router.callback_query(F.data.startswith("rs:"))
async def on_reset_callback(callback: CallbackQuery, runtime: Runtime) -> None:
    user = callback.from_user
    if user is None or not runtime.allowlist.permits(user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    choice = (callback.data or "rs:no")[3:]
    if choice != "ok":
        await callback.answer()
        if callback.message:
            await callback.message.edit_text("Очистку журнала отменил.")
        return
    entries, coverage = runtime.ledger.clear()
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Журнал очищен: удалено поступлений {entries}, окон покрытия {coverage}."
        )


@router.callback_query(F.data.startswith("wdok:"))
async def on_withdraw_confirm(callback: CallbackQuery, runtime: Runtime) -> None:
    user = callback.from_user
    if user is None or not runtime.allowlist.permits(user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    raw = (callback.data or "wdok:0")[5:]
    try:
        import_id = int(raw)
    except ValueError:
        await callback.answer("Непонятная кнопка", show_alert=True)
        return
    runtime.awaiting_withdraw.pop(user.id, None)
    removed = runtime.ledger.undo_import(import_id)
    await callback.answer()
    if callback.message is None:
        return
    if removed is None:
        await callback.message.edit_text("Этой загрузки уже нет в журнале.")
        return
    entries, coverage = removed
    await callback.message.edit_text(
        f"Убрал загрузку: поступлений {entries}, окон покрытия {coverage}."
    )


@router.callback_query(F.data.startswith("wd:"))
async def on_withdraw_pick(callback: CallbackQuery, runtime: Runtime) -> None:
    user = callback.from_user
    if user is None or not runtime.allowlist.permits(user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    choice = (callback.data or "wd:no")[3:]
    if choice == "no":
        runtime.awaiting_withdraw.pop(user.id, None)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text("Не убираю ничего.")
        return
    try:
        import_id = int(choice)
    except ValueError:
        await callback.answer("Непонятная кнопка", show_alert=True)
        return
    item = runtime.ledger.get_import(import_id)
    await callback.answer()
    if item is None:
        if callback.message:
            await callback.message.edit_text("Этой загрузки уже нет в журнале.")
        return
    if callback.message:
        await callback.message.edit_text(
            _withdraw_confirm_text(item),
            reply_markup=withdraw_confirm_keyboard(item.id),
        )


async def _offer_withdraw_by_file(
    message: Message, runtime: Runtime, *, digest: str, name: str
) -> None:
    item = runtime.ledger.find_import_by_hash(digest)
    if item is None:
        await message.answer(
            f"Файл «{escape(name)}» в журнале не найден. "
            "Либо его уже убрали, либо это загрузка до учёта файлов — "
            "выберите её списком /withdraw."
        )
        return
    await message.answer(
        _withdraw_confirm_text(item),
        reply_markup=withdraw_confirm_keyboard(item.id),
    )


async def _run(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)

    registry = ProfileRegistry.load(settings.profiles_path)
    allowlist = Allowlist(settings.allowed_ids)
    runtime = Runtime(
        settings=settings,
        allowlist=allowlist,
        limiter=RateLimiter(settings.rate_limit_seconds),
        store=Store(settings.db_path),
        ledger=Ledger(settings.db_path),
        registry=registry,
        pending={},
        awaiting_income={},
        awaiting_statement={},
        awaiting_withdraw={},
    )

    if allowlist.is_open:
        log.warning(
            "ALLOWED_USER_IDS пуст: бот отвечает всем. Заполните список, когда будет ID."
        )
    else:
        log.info("Допущены пользователи: %s", sorted(allowlist.allowed))
    if not len(registry):
        log.warning("Реестр профилей пуст. Запустите tools/build_profiles.py")
    else:
        log.info("Загружено эталонов: %s", ", ".join(registry.ids()))

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    dp.workflow_data["runtime"] = runtime
    log.info("Бот запущен, ожидание сообщений…")
    await dp.start_polling(bot)


def run_bot(settings: Settings | None = None) -> None:
    if settings is None:
        settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_run(settings))
