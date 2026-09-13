"""Проверки бота, которым не нужен живой Telegram."""

from __future__ import annotations

from pathlib import Path

from checkvalidator.bot.access import Allowlist, RateLimiter
from checkvalidator.bot.config import parse_user_ids
from checkvalidator.bot.formatters import (
    format_denied,
    format_help,
    format_ledger,
    format_report,
    is_pdf_bytes,
)
from checkvalidator.bot.storage import Store
from checkvalidator.extract import ExtractedFields
from checkvalidator.ledger import LedgerEntry
from checkvalidator.models import Report, Severity, Signal, Verdict


def test_parse_user_ids() -> None:
    assert parse_user_ids("") == set()
    assert parse_user_ids("  ") == set()
    assert parse_user_ids("1, 2;3") == {1, 2, 3}


def test_allowlist_open_until_configured() -> None:
    opened = Allowlist(set())
    assert opened.is_open
    assert opened.permits(42)
    closed = Allowlist({1, 2})
    assert not closed.is_open
    assert closed.permits(1)
    assert not closed.permits(99)


def test_rate_limiter_blocks_then_allows() -> None:
    limiter = RateLimiter(10)
    assert limiter.check(7, now=100.0) == 0.0
    wait = limiter.check(7, now=104.0)
    assert 5.9 <= wait <= 6.1
    assert limiter.check(7, now=111.0) == 0.0
    assert limiter.check(8, now=111.0) == 0.0


def test_pdf_sniff() -> None:
    assert is_pdf_bytes(b"%PDF-1.4\n")
    assert not is_pdf_bytes(b"\x89PNG\r\n")
    assert not is_pdf_bytes(b"")


def test_store_remembers_previous_verdict(tmp_path: Path) -> None:
    store = Store(tmp_path / "bot.db")
    assert store.last_by_hash("abc") is None
    store.record(
        sha256="abc",
        user_id=1,
        filename="a.pdf",
        expected="VTB/Чек",
        profile_id="VTB/Чек",
        verdict="ПОДДЕЛКА",
    )
    past = store.last_by_hash("abc")
    assert past is not None
    assert past.verdict == "ПОДДЕЛКА"
    assert past.profile_id == "VTB/Чек"


def test_store_mode_roundtrip(tmp_path: Path) -> None:
    from checkvalidator.models import CheckMode

    store = Store(tmp_path / "prefs.db")
    store.set_mode(3, CheckMode.STRUCTURE)
    assert store.get_mode(3) is CheckMode.STRUCTURE


def test_format_report_escapes_html() -> None:
    report = Report(
        verdict=Verdict.FORGED,
        profile_id="VTB/Чек",
        summary='Файл создан программой "Foo <bar>".',
        signals=[
            Signal(
                id="producer",
                passed=False,
                severity=Severity.CRITICAL,
                explanation="Создатель: <script>",
            )
        ],
    )
    text = format_report(report)
    assert "❌ ПОДДЕЛКА" in text
    assert "&lt;script&gt;" in text
    assert "<script>" not in text
    assert "критично" in text


def test_format_report_shows_extracted_amount() -> None:
    from datetime import datetime

    report = Report(
        verdict=Verdict.UNCONFIRMED,
        profile_id="VTB/Чек",
        summary="Явных признаков подделки не обнаружено.",
        fields=ExtractedFields(
            amount_kopecks=41000, occurred_at=datetime(2026, 9, 2, 13, 2)
        ),
        signals=[],
    )
    text = format_report(report)
    assert "410,00" in text
    assert "02.09.2026" in text


def test_format_help_mentions_income() -> None:
    text = format_help(open_access=True)
    assert "/income" in text
    assert "/statement" in text
    assert "/ledger_reset" in text
    assert "/withdraw" in text
    assert "ПОДТВЕРЖДЁН" in text
    assert "ПОДЛИННЫЙ" in text
    assert "по счёту" in text or "по счету" in text.replace("ё", "е")


def test_format_statement_help_asks_for_account_not_card() -> None:
    from checkvalidator.bot.formatters import format_statement_help

    text = format_statement_help()
    folded = text.lower().replace("ё", "е")
    assert "выписки по счету" in folded or "выписка по счету" in folded
    assert "по карте" in folded
    assert "т-банк" in folded or "движении средств" in folded


def test_format_ledger_lists_every_entry() -> None:
    from datetime import datetime, timedelta

    entries = [
        LedgerEntry(
            id=i,
            amount_kopecks=100 * i,
            occurred_at=datetime(2026, 9, 1) + timedelta(hours=i),
            description="",
            source="statement-pdf",
            consumed_by=None,
        )
        for i in range(1, 21)
    ]
    texts = format_ledger(entries, [])
    blob = "\n".join(texts)
    assert "Поступления" in blob
    assert "— 20" in blob
    assert blob.count("• ") == 20
    assert "1,00 ₽" in blob
    assert "20,00 ₽" in blob


def test_format_denied_includes_id() -> None:
    text = format_denied(12345)
    assert "12345" in text
    assert "Нет доступа" in text
