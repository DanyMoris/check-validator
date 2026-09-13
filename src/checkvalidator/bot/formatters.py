"""Текст ответа пользователю в Telegram (HTML)."""

from __future__ import annotations

from html import escape

from checkvalidator.extract import format_dt, format_rub
from checkvalidator.ledger import Coverage, LedgerEntry
from checkvalidator.models import CheckMode, Report, Severity, Verdict

from .storage import PastCheck

VERDICT_TITLE = {
    Verdict.CONFIRMED: "✅ ПОДТВЕРЖДЁН",
    Verdict.GENUINE: "✅ ПОДЛИННЫЙ",
    Verdict.FORGED: "❌ ПОДДЕЛКА",
    Verdict.UNCONFIRMED: "⚠️ НЕ ПОДТВЕРЖДЁН",
}

MARK = {True: "ок", False: "нет"}


def is_pdf_bytes(data: bytes) -> bool:
    return data.startswith(b"%PDF")


def format_wait(seconds: float) -> str:
    n = max(1, int(seconds + 0.5))
    return f"Подождите {n} с и пришлите файл ещё раз — слишком часто."


def format_denied(user_id: int) -> str:
    return (
        "Нет доступа к этому боту.\n"
        f"Ваш Telegram ID: <code>{user_id}</code>\n"
        "Передайте его тому, кто настраивает сервис, чтобы вас внесли в список."
    )


def format_mode_prompt(current: CheckMode | None = None) -> str:
    if current is CheckMode.LEDGER:
        now = "Сейчас: сверка с выписками (журнал)."
    elif current is CheckMode.STRUCTURE:
        now = "Сейчас: только файл, без журнала."
    else:
        now = "Сначала выберите, как проверять документы."
    return (
        f"{now}\n\n"
        "Есть ли у вас доступ к <b>выпискам по счёту</b>?\n\n"
        "• <b>Есть выписки</b> — чеки сверяются с журналом поступлений. "
        "Настоящий PDF без записи в журнале будет «НЕ ПОДТВЕРЖДЁН». "
        "Совпадение суммы и даты — «ПОДТВЕРЖДЁН».\n"
        "• <b>Нет выписок</b> — журнал не нужен. Настоящий файл банка — "
        "«ПОДЛИННЫЙ», файл после Word, Photoshop или «печати в PDF» — "
        "«ПОДДЕЛКА». Это не доказательство, что деньги пришли."
    )


def format_mode_set(mode: CheckMode) -> str:
    if mode is CheckMode.LEDGER:
        return (
            "Режим: <b>сверка со счётом</b>. "
            "Загрузите выписку командой /statement или одно поступление /income. "
            "Потом пришлите PDF чека."
        )
    return (
        "Режим: <b>только файл</b>, без журнала. "
        "Настоящий документ банка — «ПОДЛИННЫЙ», "
        "пересохранённый в другой программе — «ПОДДЕЛКА». "
        "Сменить режим: /start."
    )


def format_ledger_off() -> str:
    return (
        "Сейчас включён режим без выписок, журнал не используется. "
        "Чтобы сверять со счётом, нажмите /start и выберите «Есть выписки»."
    )


def format_income_help() -> str:
    return (
        "Добавить поступление можно так:\n"
        "• <code>/income 410 02.09.2026 13:02</code>\n"
        "• <code>/income</code> и следом CSV-выписка или PDF своей входящей справки\n"
        "• вставить SMS Сбера о зачислении (текст вида «СЧЁТ… +410р»), скопировав из сообщений на iPhone\n\n"
        "Пересылка SMS с Android не используется. SMS ВТБ и Альфы обычно не приходят: "
        "в приложении они платные и часто выключены. "
        "CSV: колонки дата и сумма (как в Excel). Две и больше строк создают "
        "покрытие периода. Журнал хранит только зачисления. "
        "Одна ручная запись или SMS покрытие не создаёт: нет прихода → «НЕ ПОДТВЕРЖДЁН», "
        "а не подделка."
    )


def format_statement_help() -> str:
    return (
        "Пришлите CSV или PDF <b>выписки по счёту за период</b> "
        "(ВТБ — по счёту, Альфа — по счёту, Сбер — по платёжному счёту, "
        "Т-Банк — справка о движении средств). "
        "Выписку <b>по карте</b> не присылайте: в ней нет переводов СБП на счёт, "
        "в журнал её не записываю.\n\n"
        "Зачисления попадут в журнал, расходы нет. Повтор той же суммы в тот же день "
        "из CSV и PDF не создаёт вторую свободную запись. Если новая выписка шире "
        "уже загруженной, в журнал попадут только операции за дни вне старого периода. "
        "Пересекающиеся окна покрытия сливаются в одно.\n\n"
        "Выписку по счёту можно просто прислать файлом — бот сам поймёт, что это не чек.\n"
        "Один файл на команду /statement. Ещё одну выписку — снова /statement или /income.\n"
        "Чек отправителя (исходящий перевод) в журнал не записывается.\n"
        "Одна справка по операции покрытие не создаёт."
    )


def format_coverage_help() -> str:
    return (
        "Отметить, что выписка за период полная:\n"
        "<code>/coverage 01.09.2026 11.09.2026</code>\n"
        "Внутри этого окна сверка ищет поступление в журнале. Если его нет, "
        "ответ будет «НЕ ПОДТВЕРЖДЁН», а не подделка: структура документа "
        "проверяется отдельно."
    )


def format_reset_help(entries: int, coverage: int) -> str:
    return (
        f"В журнале сейчас поступлений: {entries}, окон покрытия: {coverage}.\n"
        "Очистка удалит все записи, в том числе ручные /income, и снимет "
        "пометки «уже сопоставлено» с чеками.\n"
        "Это нельзя отменить. Подтвердите кнопкой."
    )


def format_withdraw_help() -> str:
    return (
        "Убрать из журнала ошибочный CSV или PDF: выберите загрузку ниже "
        "или пришлите <b>тот же файл</b> ещё раз — найду его по содержимому.\n"
        "Ручные /income так не снимаются, для полной очистки — /ledger_reset."
    )


TELEGRAM_MESSAGE_LIMIT = 3900


def split_html_messages(lines: list[str], limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Режет длинный ответ на несколько сообщений Telegram."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if buf else 0)
        if buf and size + extra > limit:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += extra
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [""]


def format_ledger(entries: list[LedgerEntry], coverage: list[Coverage]) -> list[str]:
    """Все поступления, без обрезки. При необходимости несколько сообщений."""
    if not entries and not coverage:
        return ["Журнал пуст. Добавьте поступление командой /income."]
    lines = [f"<b>Поступления</b> — {len(entries)}"]
    if not entries:
        lines.append("записей нет")
    for entry in entries:
        used = "уже сопоставлено" if entry.consumed_by else "свободно"
        lines.append(
            f"• {format_rub(entry.amount_kopecks)} · {format_dt(entry.occurred_at)} · "
            f"{escape(entry.source)} · {used}"
        )
    if coverage:
        lines.append("")
        lines.append(f"<b>Покрытие выписки</b> — {len(coverage)}")
        for cov in coverage:
            lines.append(f"• {format_dt(cov.start_at)} – {format_dt(cov.end_at)}")
    return split_html_messages(lines)


def format_help(open_access: bool) -> str:
    lock = (
        "Сейчас бот открыт для всех. Задайте ALLOWED_USER_IDS в файле .env, "
        "чтобы пускать только своих."
        if open_access
        else "Бот доступен только людям из списка."
    )
    return (
        "<b>Проверка банковского чека, квитанции или справки</b>\n\n"
        "<b>Два режима</b> — выбираются после /start.\n"
        "• <b>Есть выписки</b> — сверка с журналом: "
        "<b>ПОДТВЕРЖДЁН</b> / <b>НЕ ПОДТВЕРЖДЁН</b> / <b>ПОДДЕЛКА</b>.\n"
        "• <b>Нет выписок</b> — только файл: настоящий PDF банка — "
        "<b>ПОДЛИННЫЙ</b>, после Word/Photoshop/«печати в PDF» — "
        "<b>ПОДДЕЛКА</b>. Это не значит, что деньги пришли.\n\n"
        "Пришлите PDF из приложения банка. Скриншоты не принимаются.\n"
        "После загрузки можно указать банк и тип — сверка будет строже.\n\n"
        "<b>Сверка со счётом</b>\n"
        "Пришлите /income и сумму с датой, SMS Сбера о зачислении — или CSV/PDF "
        "<b>выписки по счёту</b> (не по карте). После этого чек "
        "с той же суммой и датой (±2 суток) станет «ПОДТВЕРЖДЁН». "
        "В журнал идут только зачисления. Не загружайте свои исходящие чеки.\n\n"
        f"{lock}\n\n"
        "Команды: /start — это сообщение, /id — ваш номер в Telegram, "
        "/profiles — какие эталоны уже собраны, /income — добавить поступление, "
        "/statement — загрузить выписку или входящую справку, "
        "/coverage — отметить период выписки, /ledger — журнал, "
        "/withdraw — убрать ошибочный CSV или PDF, "
        "/ledger_reset — очистить журнал (с подтверждением), /cancel — отмена."
    )


def format_report(report: Report, *, previous: PastCheck | None = None) -> str:
    lines = [f"<b>{VERDICT_TITLE[report.verdict]}</b>"]
    if report.profile_id:
        lines.append(f"Эталон: <code>{escape(report.profile_id)}</code>")
    if report.fields is not None:
        bits = []
        if report.fields.amount_kopecks is not None:
            bits.append(f"Сумма: {format_rub(report.fields.amount_kopecks)}")
        if report.fields.occurred_at is not None:
            bits.append(f"Дата: {format_dt(report.fields.occurred_at)}")
        if bits:
            lines.append(" · ".join(bits))
    lines.append("")
    lines.append(escape(report.summary))

    failures = [s for s in report.signals if s.failed]
    if report.verdict is Verdict.CONFIRMED or report.verdict is Verdict.GENUINE:
        shown = [
            s
            for s in report.signals
            if s.id.startswith("gost_") or s.id.startswith("ledger_") or s.failed
        ]
    else:
        shown = failures or [s for s in report.signals if s.id == "profile_known"]
    if shown:
        lines.append("")
        lines.append("<b>Проверки</b>")
        for signal in shown[:12]:
            flag = MARK[signal.passed]
            extra = ""
            if signal.severity is Severity.CRITICAL and signal.failed:
                extra = " — критично"
            elif signal.severity is Severity.WARN and signal.failed:
                extra = " — подозрительно"
            lines.append(f"• [{flag}] {escape(signal.explanation)}{extra}")

    if previous is not None:
        lines.append("")
        lines.append(
            "Этот же файл уже проверяли "
            f"{escape(previous.created_at)} — тогда вердикт был "
            f"<b>{escape(previous.verdict)}</b>."
        )

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3890] + "\n…"
    return text
