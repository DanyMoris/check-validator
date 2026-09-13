"""Командная строка: проверка документа и просмотр реестра профилей."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import analyse
from .extract import extract, format_dt, format_rub
from .ledger import Ledger, parse_coverage_args, parse_income_args
from .models import CheckMode, Report, Severity, Verdict
from .profiles import ProfileRegistry

DEFAULT_DB = Path("data") / "bot.db"

MARK = {True: "  ок  ", False: "  !!  "}
VERDICT_LINE = {
    Verdict.CONFIRMED: "ПОДТВЕРЖДЁН",
    Verdict.GENUINE: "ПОДЛИННЫЙ",
    Verdict.FORGED: "ПОДДЕЛКА",
    Verdict.UNCONFIRMED: "НЕ ПОДТВЕРЖДЁН",
}


def _print_report(path: Path, report: Report, verbose: bool) -> None:
    print(f"\n{path.name}")
    print(f"  вердикт: {VERDICT_LINE[report.verdict]}")
    if report.profile_id:
        print(f"  эталон:  {report.profile_id}")
    if report.fields is not None:
        if report.fields.amount_kopecks is not None:
            print(f"  сумма:   {format_rub(report.fields.amount_kopecks)}")
        if report.fields.occurred_at is not None:
            print(f"  дата:    {format_dt(report.fields.occurred_at)}")
        if report.fields.operation_id:
            print(f"  операция:{report.fields.operation_id}")
    print(f"  {report.summary}")

    shown = report.signals if verbose else [s for s in report.signals if s.failed]
    if shown:
        print("\n  проверки:")
        for s in shown:
            sev = "" if s.severity is Severity.INFO else f" [{s.severity.value}]"
            print(f"   {MARK[s.passed]} {s.id}{sev}: {s.explanation}")
            if verbose and s.evidence:
                for k, v in s.evidence.items():
                    print(f"          {k} = {v}")


def cmd_check(args: argparse.Namespace) -> int:
    registry = ProfileRegistry.load(args.profiles)
    if not len(registry):
        print(
            "Реестр профилей пуст. Соберите его: python tools/build_profiles.py",
            file=sys.stderr,
        )

    paths: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        paths.extend(sorted(p.rglob("*.pdf")) if p.is_dir() else [p])

    if not paths:
        print("Не найдено ни одного PDF", file=sys.stderr)
        return 2

    worst = 0
    for path in paths:
        if not path.exists():
            print(f"{path}: файл не найден", file=sys.stderr)
            worst = max(worst, 2)
            continue
        ledger = Ledger(Path(args.ledger)) if args.ledger else None
        report = analyse(
            path,
            registry,
            expected=args.expect,
            ledger=ledger,
            consume=args.consume,
            mode=args.mode,
        )
        _print_report(path, report, args.verbose)
        if report.verdict is Verdict.FORGED:
            worst = max(worst, 1)
    print()
    return worst


def cmd_profiles(args: argparse.Namespace) -> int:
    registry = ProfileRegistry.load(args.profiles)
    if not len(registry):
        print("Профилей нет. Соберите их: python tools/build_profiles.py")
        return 1
    print(f"Профилей в реестре: {len(registry)}\n")
    for p in sorted(registry, key=lambda x: x.id):
        flag = "" if p.established else "  (предварительный)"
        print(f"{p.id}{flag}")
        print(f"   образцов   {p.samples}")
        print(f"   producer   {', '.join(str(x) for x in p.producers)}")
        print(f"   объектов   {p.objects.min}–{p.objects.max}")
        print(f"   подпись    {'да' if p.signature_expected else 'нет'}")
        print(f"   картинки   {p.has_image}")
        if p.quirks:
            for q in p.quirks:
                print(f"   особенность: {q}")
        print()
    return 0


def cmd_bot(_args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from .bot.app import run_bot
    from .bot.config import Settings

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        print(
            "Не задан BOT_TOKEN.\n"
            "1. Скопируйте .env.example в .env\n"
            "2. Вставьте токен, который выдаст @BotFather\n"
            "Подробности — в README.md",
            file=sys.stderr,
        )
        return 2
    run_bot(settings)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    worst = 0
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            files = sorted(path.rglob("*.pdf"))
        else:
            files = [path]
        for pdf in files:
            if not pdf.exists():
                print(f"{pdf}: файл не найден", file=sys.stderr)
                worst = 2
                continue
            fields = extract(pdf)
            amount = (
                format_rub(fields.amount_kopecks)
                if fields.amount_kopecks is not None
                else "—"
            )
            when = format_dt(fields.occurred_at) if fields.occurred_at else "—"
            print(f"{pdf}: {amount}  {when}")
            if fields.operation_id:
                print(f"    операция {fields.operation_id}")
    return worst


def cmd_income_add(args: argparse.Namespace) -> int:
    parsed = parse_income_args(" ".join(args.text))
    if parsed is None:
        print("Нужны сумма и дата, например: 410 02.09.2026 13:02", file=sys.stderr)
        return 2
    amount, occurred, comment = parsed
    Ledger(Path(args.db)).add_entry(amount, occurred, description=comment, source="manual")
    extra = f" ({comment})" if comment else ""
    print(f"записал {format_rub(amount)} за {format_dt(occurred)}{extra}")
    return 0


def cmd_income_csv(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"{path}: файл не найден", file=sys.stderr)
        return 2
    result = Ledger(Path(args.db)).import_csv(path.read_bytes(), filename=path.name)
    if not result.count and not result.skipped and result.coverage is None:
        print("в файле нет строк с датой и суммой", file=sys.stderr)
        return 1
    print(f"добавлено поступлений: {result.count}")
    if result.skipped:
        print(f"уже были в журнале: {result.skipped}")
    if result.debit_count:
        print(f"расходов пропущено: {result.debit_count}")
    if result.coverage:
        print(f"покрытие: {format_dt(result.coverage.start_at)} – {format_dt(result.coverage.end_at)}")
    return 0


def cmd_income_list(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.db))
    entries = ledger.list_entries()
    if not entries:
        print("журнал пуст")
        return 0
    print(f"поступлений: {len(entries)}")
    for entry in entries:
        used = "занято" if entry.consumed_by else "свободно"
        print(
            f"{format_dt(entry.occurred_at):<20} {format_rub(entry.amount_kopecks):>14}  "
            f"{entry.source:<14} {used}"
        )
    for cov in ledger.list_coverage():
        print(f"покрытие {format_dt(cov.start_at)} – {format_dt(cov.end_at)}")
    return 0


def cmd_income_cover(args: argparse.Namespace) -> int:
    parsed = parse_coverage_args(" ".join(args.text))
    if parsed is None:
        print("Нужны две даты, например: 01.09.2026 11.09.2026", file=sys.stderr)
        return 2
    start, end = parsed
    Ledger(Path(args.db)).add_coverage(start, end, source="manual")
    print(f"покрытие {format_dt(start)} – {format_dt(end)}")
    return 0


def cmd_income_pdf(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"{path}: файл не найден", file=sys.stderr)
        return 2
    result = Ledger(Path(args.db)).import_pdf(path, filename=path.name)
    if result.count == 0 and result.coverage is None and not result.skipped:
        print(result.warning or "не прочитал сумму и дату", file=sys.stderr)
        return 1
    if result.is_statement:
        print(
            f"выписка: зачислений {result.count}, "
            f"расходов пропущено {result.debit_count}"
        )
    else:
        print(f"добавлено поступлений: {result.count}")
    if result.skipped:
        print(f"уже были в журнале: {result.skipped}")
    for fields in result.fields:
        if fields.amount_kopecks is not None and fields.occurred_at is not None:
            print(f"  {format_rub(fields.amount_kopecks)}  {format_dt(fields.occurred_at)}")
    if result.coverage:
        print(f"покрытие: {format_dt(result.coverage.start_at)} – {format_dt(result.coverage.end_at)}")
    if result.warning:
        print(result.warning)
    return 0


def cmd_income_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Очистка журнала необратима. Повторите с --yes.", file=sys.stderr)
        return 2
    entries, coverage = Ledger(Path(args.db)).clear()
    print(f"удалено поступлений: {entries}, покрытий: {coverage}")
    return 0


def cmd_income_undo(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.db))
    import_id = args.id
    if import_id is None:
        items = ledger.list_imports(1)
        if not items:
            print("нет загруженных выписок", file=sys.stderr)
            return 1
        import_id = items[0].id
    removed = ledger.undo_import(import_id)
    if removed is None:
        print("такой загрузки нет", file=sys.stderr)
        return 1
    entries, coverage = removed
    print(f"убрано поступлений: {entries}, покрытий: {coverage}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Консоль Windows по умолчанию в cp866 и калечит кириллицу.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="checkval", description="Проверка подлинности банковских документов (PDF)"
    )
    parser.add_argument("--profiles", default=None, help="путь к profiles.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="проверить документ или папку")
    p_check.add_argument("paths", nargs="+")
    p_check.add_argument(
        "--expect",
        metavar="БАНК/ТИП",
        default=None,
        help="за какой документ он себя выдаёт, например VTB/Чек — сверка станет строже",
    )
    p_check.add_argument("-v", "--verbose", action="store_true", help="показать все проверки")
    p_check.add_argument("--ledger", default=None, help="путь к SQLite журнала поступлений")
    p_check.add_argument(
        "--consume",
        action="store_true",
        help="пометить найденное поступление как использованное",
    )
    p_check.add_argument(
        "--mode",
        choices=[CheckMode.LEDGER.value, CheckMode.STRUCTURE.value],
        default=CheckMode.LEDGER.value,
        help="ledger — сверка с журналом; structure — только шаблон файла",
    )
    p_check.set_defaults(func=cmd_check)

    p_prof = sub.add_parser("profiles", help="показать реестр эталонов")
    p_prof.set_defaults(func=cmd_profiles)

    p_bot = sub.add_parser("bot", help="запустить Telegram-бота")
    p_bot.set_defaults(func=cmd_bot)

    p_extract = sub.add_parser("extract", help="показать сумму и дату из PDF")
    p_extract.add_argument("paths", nargs="+")
    p_extract.set_defaults(func=cmd_extract)

    p_inc = sub.add_parser("income", help="журнал поступлений")
    inc = p_inc.add_subparsers(dest="income_command", required=True)
    p_add = inc.add_parser("add", help="добавить одно поступление")
    p_add.add_argument("text", nargs="+", help="сумма и дата, например 410 02.09.2026 13:02")
    p_add.add_argument("--db", default=str(DEFAULT_DB))
    p_add.set_defaults(func=cmd_income_add)
    p_csv = inc.add_parser("csv", help="загрузить CSV-выписку")
    p_csv.add_argument("path")
    p_csv.add_argument("--db", default=str(DEFAULT_DB))
    p_csv.set_defaults(func=cmd_income_csv)
    p_list = inc.add_parser("list", help="показать журнал")
    p_list.add_argument("--db", default=str(DEFAULT_DB))
    p_list.set_defaults(func=cmd_income_list)
    p_cov = inc.add_parser("cover", help="отметить период полной выписки")
    p_cov.add_argument("text", nargs="+", help="даты, например 01.09.2026 11.09.2026")
    p_cov.add_argument("--db", default=str(DEFAULT_DB))
    p_cov.set_defaults(func=cmd_income_cover)
    p_pdf = inc.add_parser("pdf", help="загрузить входящую справку или выписку PDF")
    p_pdf.add_argument("path")
    p_pdf.add_argument("--db", default=str(DEFAULT_DB))
    p_pdf.set_defaults(func=cmd_income_pdf)
    p_reset = inc.add_parser("reset", help="очистить журнал поступлений")
    p_reset.add_argument("--yes", action="store_true", help="подтвердить очистку")
    p_reset.add_argument("--db", default=str(DEFAULT_DB))
    p_reset.set_defaults(func=cmd_income_reset)
    p_undo = inc.add_parser("undo", help="убрать последнюю CSV/PDF-загрузку")
    p_undo.add_argument("--id", type=int, default=None, help="id загрузки из журнала")
    p_undo.add_argument("--db", default=str(DEFAULT_DB))
    p_undo.set_defaults(func=cmd_income_undo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
