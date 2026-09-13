"""Журнал реальных поступлений и сверка с полями документа.

Пополнение двумя путями:
- ручная запись или одиночный входящий PDF — это факт «деньги были»,
  но не факт «других денег за этот день не было». Покрытия периода нет.
- CSV-выписка из двух и более строк — создаёт покрытие: если внутри него
  поступления с такой суммой и датой нет, документ объявляется подделкой.

Одна запись журнала может подтвердить только один файл (consumed_by).
"""

from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .extract import (
    ExtractedFields,
    _AMOUNT_TOKEN,
    document_direction,
    extract_operations,
    extract_pdf_text,
    format_dt,
    format_rub,
    parse_amount,
    parse_datetime,
    parse_signed_amount,
    take_datetime,
)
from .models import Severity, Signal
from .statements import CARD_STATEMENT_HINT, parse_statement
from .sms import parse_sber_sms

MATCH_WINDOW = timedelta(hours=48)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: int
    amount_kopecks: int
    occurred_at: datetime
    description: str
    source: str
    consumed_by: str | None
    import_id: int | None = None


@dataclass(frozen=True, slots=True)
class Coverage:
    id: int
    start_at: datetime
    end_at: datetime
    source: str
    import_id: int | None = None


@dataclass(frozen=True, slots=True)
class LedgerImport:
    id: int
    source: str
    filename: str
    file_sha256: str | None
    created_at: datetime
    entry_count: int
    coverage_count: int
    consumed_count: int = 0

    @property
    def label(self) -> str:
        name = self.filename.strip() or self.source
        return name


@dataclass(frozen=True, slots=True)
class MatchResult:
    hit: LedgerEntry | None = None
    covered: bool = False
    reused: bool = False
    coverage: Coverage | None = None
    window: timedelta = MATCH_WINDOW

    @property
    def confirmed(self) -> bool:
        return self.hit is not None and not self.reused


@dataclass(frozen=True, slots=True)
class PdfImportResult:
    count: int
    coverage: Coverage | None
    fields: tuple[ExtractedFields, ...]
    direction: str
    warning: str | None = None
    is_statement: bool = False
    debit_count: int = 0
    skipped: int = 0
    import_id: int | None = None


@dataclass(frozen=True, slots=True)
class SmsImportResult:
    count: int
    fields: ExtractedFields | None = None
    skipped: int = 0
    warning: str | None = None
    import_id: int | None = None


@dataclass(frozen=True, slots=True)
class CsvImportResult:
    count: int
    coverage: Coverage | None
    skipped: int = 0
    debit_count: int = 0
    import_id: int | None = None


@dataclass(frozen=True, slots=True)
class _CsvOperation:
    occurred: datetime
    amount_kopecks: int
    comment: str
    is_credit: bool
    include: bool


def parse_csv_bytes(data: bytes) -> list[tuple[datetime, int, str]]:
    text = _decode(data)
    return parse_csv_text(text)


def parse_csv_text(text: str) -> list[tuple[datetime, int, str]]:
    """Зачисления из выписки: дата, сумма, необязательный комментарий.

    Списания, отклонённые и ещё не выполненные строки не возвращаются.
    Понимает заголовок (дата/сумма) и файлы без него. Разделитель — запятая
    или точка с запятой, как выгружает Excel.
    """
    return [
        (op.occurred, op.amount_kopecks, op.comment)
        for op in _parse_csv_operations(text)
        if op.is_credit and op.include
    ]


def _parse_csv_operations(text: str) -> list[_CsvOperation]:
    sample = text.lstrip("\ufeff")
    lines = [ln for ln in sample.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return []
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = [[c.strip() for c in row] for row in reader if any(c.strip() for c in row)]
    if not rows:
        return []

    header = [c.lower() for c in rows[0]]
    date_idx, amount_idx, comment_idx = 0, 1, 2
    direction_idx = None
    status_idx = None
    has_header = False
    if _looks_like_header(header):
        has_header = True
        date_idx = _header_index(header, ("дата", "date", "datetime", "время")) or 0
        amount_idx = _amount_column_index(header) or 1
        comment_idx = (
            _header_index(header, ("описание", "комментарий", "comment", "назначение", "наименование"))
            or 2
        )
        direction_idx = _header_index(
            header, ("списание/зачисление", "зачисление", "списание", "direction")
        )
        status_idx = _header_index(header, ("статус", "status"))

    ops: list[_CsvOperation] = []
    body = rows[1:] if has_header else rows
    for row in body:
        if max(date_idx, amount_idx) >= len(row):
            continue
        occurred = parse_datetime(row[date_idx])
        signed = parse_signed_amount(row[amount_idx])
        if occurred is None or signed is None or signed == 0:
            continue
        comment = row[comment_idx] if comment_idx < len(row) else ""
        direction_cell = row[direction_idx] if direction_idx is not None and direction_idx < len(row) else ""
        status_cell = row[status_idx] if status_idx is not None and status_idx < len(row) else ""
        is_credit = _csv_is_credit(signed, direction_cell)
        include = _csv_is_completed(status_cell)
        ops.append(
            _CsvOperation(occurred, abs(signed), comment, is_credit=is_credit, include=include)
        )
    return ops


def parse_income_args(text: str) -> tuple[int, datetime, str] | None:
    """Разбор «410 02.09.2026 13:02 кофе» после команды /income."""
    raw = text.strip()
    if raw.lower().startswith("/income"):
        raw = raw[7:].strip()
    if not raw:
        return None
    occurred, remainder = take_datetime(raw)
    if occurred is None:
        return None
    amount = parse_amount(remainder)
    if amount is None:
        return None
    comment = remainder
    token = _AMOUNT_TOKEN.search(remainder)
    if token:
        comment = (remainder[: token.start()] + remainder[token.end() :]).strip(" ,;")
    return amount, occurred, comment.strip()


def parse_coverage_args(text: str) -> tuple[datetime, datetime] | None:
    """Разбор «01.09.2026 11.09.2026» — вторая дата до конца дня, если время не указано."""
    raw = text.strip()
    if raw.lower().startswith("/coverage"):
        raw = raw[9:].strip()
    start, rest = take_datetime(raw)
    end, leftover = take_datetime(rest)
    if start is None or end is None:
        return None
    if end.hour == 0 and end.minute == 0 and end.second == 0 and not leftover.startswith(":"):
        end = end.replace(hour=23, minute=59, second=59)
    return start, end


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY,
                    amount_kopecks INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    consumed_by TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_amount ON ledger_entries(amount_kopecks)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_coverage (
                    id INTEGER PRIMARY KEY,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_imports (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    filename TEXT NOT NULL DEFAULT '',
                    file_sha256 TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            _ensure_column(conn, "ledger_entries", "import_id", "INTEGER")
            _ensure_column(conn, "ledger_coverage", "import_id", "INTEGER")
            _backfill_legacy_imports(conn)

    def add_entry(
        self,
        amount_kopecks: int,
        occurred_at: datetime,
        *,
        description: str = "",
        source: str = "manual",
        import_id: int | None = None,
    ) -> LedgerEntry:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ledger_entries
                    (amount_kopecks, occurred_at, description, source, consumed_by, created_at, import_id)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    amount_kopecks,
                    occurred_at.isoformat(timespec="seconds"),
                    description,
                    source,
                    stamp,
                    import_id,
                ),
            )
            entry_id = int(cur.lastrowid)
        return LedgerEntry(
            entry_id, amount_kopecks, occurred_at, description, source, None, import_id
        )

    def find_same_day(
        self, amount_kopecks: int, occurred_at: datetime
    ) -> LedgerEntry | None:
        """Та же сумма в тот же календарный день — CSV с временем и PDF без него."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, amount_kopecks, occurred_at, description, source, consumed_by, import_id
                FROM ledger_entries
                WHERE amount_kopecks = ?
                """,
                (amount_kopecks,),
            ).fetchall()
        target = occurred_at.date()
        for entry in (_row_to_entry(r) for r in rows):
            if entry.occurred_at.date() == target:
                return entry
        return None

    def _same_day_tally(self) -> Counter[tuple[int, date]]:
        """Сколько записей уже есть на каждую пару сумма+день."""
        tally: Counter[tuple[int, date]] = Counter()
        for entry in self.list_entries():
            tally[(entry.amount_kopecks, entry.occurred_at.date())] += 1
        return tally

    def add_coverage(
        self,
        start_at: datetime,
        end_at: datetime,
        source: str = "manual",
        import_id: int | None = None,
    ) -> Coverage:
        """Добавляет покрытие, сливая пересекающиеся окна в одно (без «братьев»)."""
        coverage, _created = self._ensure_coverage(start_at, end_at, source, import_id)
        return coverage

    def _ensure_coverage(
        self,
        start_at: datetime,
        end_at: datetime,
        source: str = "manual",
        import_id: int | None = None,
    ) -> tuple[Coverage, bool]:
        if end_at < start_at:
            start_at, end_at = end_at, start_at
        existing = self.list_coverage(limit=10_000)
        overlapping = [c for c in existing if _intervals_overlap(start_at, end_at, c.start_at, c.end_at)]
        contained = [c for c in overlapping if c.start_at <= start_at and c.end_at >= end_at]
        if contained:
            return contained[0], False
        if overlapping:
            union_start = min(start_at, *(c.start_at for c in overlapping))
            union_end = max(end_at, *(c.end_at for c in overlapping))
            keep = min(overlapping, key=lambda c: c.id)
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE ledger_coverage
                    SET start_at = ?, end_at = ?
                    WHERE id = ?
                    """,
                    (
                        union_start.isoformat(timespec="seconds"),
                        union_end.isoformat(timespec="seconds"),
                        keep.id,
                    ),
                )
                for extra in overlapping:
                    if extra.id != keep.id:
                        conn.execute("DELETE FROM ledger_coverage WHERE id = ?", (extra.id,))
            return Coverage(keep.id, union_start, union_end, keep.source, keep.import_id), True
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ledger_coverage (start_at, end_at, source, created_at, import_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    start_at.isoformat(timespec="seconds"),
                    end_at.isoformat(timespec="seconds"),
                    source,
                    stamp,
                    import_id,
                ),
            )
            cov_id = int(cur.lastrowid)
        return Coverage(cov_id, start_at, end_at, source, import_id), True

    def import_csv(
        self,
        data: bytes | str,
        *,
        source: str = "csv",
        with_coverage: bool = True,
        filename: str = "",
        file_sha256: str | None = None,
    ) -> CsvImportResult:
        text = _decode(data) if isinstance(data, bytes) else data
        ops = _parse_csv_operations(text)
        if not ops:
            return CsvImportResult(0, None)
        digest = file_sha256 or _fingerprint(data)
        import_id = self._begin_import(source, filename=filename, file_sha256=digest)
        existing = self._same_day_tally()
        seen: Counter[tuple[int, date]] = Counter()
        added = 0
        skipped = 0
        for op in ops:
            if not op.is_credit or not op.include:
                continue
            if _already_have_same_day(op.amount_kopecks, op.occurred, existing, seen):
                skipped += 1
                continue
            self.add_entry(
                op.amount_kopecks,
                op.occurred,
                description=op.comment,
                source=source,
                import_id=import_id,
            )
            added += 1
        coverage = None
        if with_coverage and len(ops) >= 2:
            start = min(op.occurred for op in ops)
            end = max(op.occurred for op in ops)
            coverage = self.add_coverage(start, end, source=source, import_id=import_id)
        if added == 0 and coverage is None:
            self._drop_import(import_id)
            import_id = None
        debit_count = sum(1 for op in ops if not op.is_credit)
        return CsvImportResult(
            added, coverage, skipped=skipped, debit_count=debit_count, import_id=import_id
        )

    def import_pdf(
        self,
        path: str | Path,
        *,
        source: str = "statement-pdf",
        with_coverage: bool = True,
        filename: str = "",
        file_sha256: str | None = None,
    ) -> PdfImportResult:
        """Кладёт в журнал зачисления из PDF входящей справки или выписки."""
        pdf_path = Path(path)
        try:
            text = extract_pdf_text(pdf_path)
            raw = pdf_path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            return PdfImportResult(
                0, None, (), "unknown", f"не удалось прочитать PDF ({type(exc).__name__})"
            )
        digest = file_sha256 or hashlib.sha256(raw).hexdigest()
        label = filename or pdf_path.name

        statement = parse_statement(pdf_path)
        if statement is not None:
            if statement.kind == "vtb_card":
                return PdfImportResult(
                    0,
                    None,
                    (),
                    "incoming",
                    CARD_STATEMENT_HINT,
                    is_statement=True,
                    debit_count=len(statement.debits),
                )
            warning = None
            if statement.debits and not statement.credits:
                warning = (
                    f"В выписке {len(statement.debits)} расходных операций и ни одного "
                    "зачисления — в журнал поступлений ничего не записано."
                )
            import_id = self._begin_import(source, filename=label, file_sha256=digest)
            existing = self._same_day_tally()
            seen: Counter[tuple[int, date]] = Counter()
            added: list[ExtractedFields] = []
            skipped = 0
            for op in statement.credits:
                assert op.amount_kopecks is not None and op.occurred_at is not None
                if _already_have_same_day(op.amount_kopecks, op.occurred_at, existing, seen):
                    skipped += 1
                    continue
                self.add_entry(
                    op.amount_kopecks,
                    op.occurred_at,
                    description=op.operation_id or "",
                    source=source,
                    import_id=import_id,
                )
                added.append(op)
            coverage = None
            if with_coverage and statement.period_start and statement.period_end:
                coverage = self.add_coverage(
                    statement.period_start,
                    statement.period_end,
                    source=source,
                    import_id=import_id,
                )
            elif with_coverage and len(statement.credits) >= 2:
                start = min(op.occurred_at for op in statement.credits if op.occurred_at)
                end = max(op.occurred_at for op in statement.credits if op.occurred_at)
                coverage = self.add_coverage(start, end, source=source, import_id=import_id)
            if not added and coverage is None:
                self._drop_import(import_id)
                import_id = None
            return PdfImportResult(
                count=len(added),
                coverage=coverage,
                fields=tuple(added),
                direction="incoming",
                warning=warning,
                is_statement=True,
                debit_count=len(statement.debits),
                skipped=skipped,
                import_id=import_id,
            )

        direction = document_direction(text)
        if direction == "outgoing":
            return PdfImportResult(
                0,
                None,
                (),
                "outgoing",
                "Это чек отправителя (исходящий перевод). Журнал учитывает только "
                "полученные деньги — в поступления не записываю. Пришлите входящую "
                "справку или выписку, где деньги пришли вам.",
            )
        warning = None
        ops = extract_operations(pdf_path)
        usable = [op for op in ops if op.usable_for_match]
        if not usable:
            return PdfImportResult(0, None, (), direction, "не прочитал сумму и дату")
        import_id = self._begin_import(source, filename=label, file_sha256=digest)
        existing = self._same_day_tally()
        seen: Counter[tuple[int, date]] = Counter()
        added = []
        skipped = 0
        for op in usable:
            assert op.amount_kopecks is not None and op.occurred_at is not None
            if _already_have_same_day(op.amount_kopecks, op.occurred_at, existing, seen):
                skipped += 1
                continue
            self.add_entry(
                op.amount_kopecks,
                op.occurred_at,
                description=op.operation_id or "",
                source=source,
                import_id=import_id,
            )
            added.append(op)
        coverage = None
        if with_coverage and len(usable) >= 2:
            start = min(op.occurred_at for op in usable if op.occurred_at is not None)
            end = max(op.occurred_at for op in usable if op.occurred_at is not None)
            coverage = self.add_coverage(start, end, source=source, import_id=import_id)
        if not added and coverage is None:
            self._drop_import(import_id)
            import_id = None
        return PdfImportResult(
            len(added),
            coverage,
            tuple(added),
            direction,
            warning,
            skipped=skipped,
            import_id=import_id,
        )

    def import_sms(
        self,
        text: str,
        *,
        received_at: datetime | None = None,
        source: str = "sms",
    ) -> SmsImportResult:
        """Одно зачисление из SMS Сбера. Списание и чужой текст не пишет."""
        parsed = parse_sber_sms(text, received_at=received_at)
        if not parsed.found:
            return SmsImportResult(0, warning="не похоже на SMS Сбера о движении по счёту")
        if not parsed.is_credit or parsed.fields is None:
            return SmsImportResult(
                0,
                warning="Это списание или SMS без суммы зачисления — в журнал не записываю.",
            )
        op = parsed.fields
        assert op.amount_kopecks is not None and op.occurred_at is not None
        existing = self._same_day_tally()
        seen: Counter[tuple[int, date]] = Counter()
        if _already_have_same_day(op.amount_kopecks, op.occurred_at, existing, seen):
            return SmsImportResult(0, fields=op, skipped=1)
        entry = self.add_entry(
            op.amount_kopecks,
            op.occurred_at,
            description=op.payer or "",
            source=source,
        )
        return SmsImportResult(1, fields=op, import_id=entry.import_id)

    def consume(self, entry_id: int, doc_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE ledger_entries SET consumed_by = ? WHERE id = ? AND consumed_by IS NULL",
                (doc_hash, entry_id),
            )

    def counts(self) -> tuple[int, int]:
        """Число поступлений и окон покрытия."""
        with self._connect() as conn:
            entries = int(conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0])
            coverage = int(conn.execute("SELECT COUNT(*) FROM ledger_coverage").fetchone()[0])
        return entries, coverage

    def clear(self) -> tuple[int, int]:
        """Удаляет все поступления, покрытие и историю загрузок выписок."""
        with self._connect() as conn:
            entries = conn.execute("DELETE FROM ledger_entries").rowcount
            coverage = conn.execute("DELETE FROM ledger_coverage").rowcount
            conn.execute("DELETE FROM ledger_imports")
        return int(entries), int(coverage)

    def get_import(self, import_id: int) -> LedgerImport | None:
        items = self.list_imports(limit=1000)
        for item in items:
            if item.id == import_id:
                return item
        return None

    def find_import_by_hash(self, file_sha256: str) -> LedgerImport | None:
        if not file_sha256:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM ledger_imports
                WHERE file_sha256 = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (file_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self.get_import(int(row["id"]))

    def list_imports(self, limit: int = 10) -> list[LedgerImport]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.id, i.source, i.filename, i.file_sha256, i.created_at,
                    (SELECT COUNT(*) FROM ledger_entries e WHERE e.import_id = i.id) AS entry_count,
                    (SELECT COUNT(*) FROM ledger_coverage c WHERE c.import_id = i.id) AS coverage_count,
                    (SELECT COUNT(*) FROM ledger_entries e
                     WHERE e.import_id = i.id AND e.consumed_by IS NOT NULL) AS consumed_count
                FROM ledger_imports i
                ORDER BY i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_import(r) for r in rows]

    def undo_import(self, import_id: int) -> tuple[int, int] | None:
        """Убирает поступления и покрытие этой загрузки. None — нет такого импорта."""
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM ledger_imports WHERE id = ?", (import_id,)
            ).fetchone()
            if exists is None:
                return None
            entries = conn.execute(
                "DELETE FROM ledger_entries WHERE import_id = ?", (import_id,)
            ).rowcount
            coverage = conn.execute(
                "DELETE FROM ledger_coverage WHERE import_id = ?", (import_id,)
            ).rowcount
            conn.execute("DELETE FROM ledger_imports WHERE id = ?", (import_id,))
        return int(entries), int(coverage)

    def _begin_import(self, source: str, *, filename: str, file_sha256: str | None) -> int:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ledger_imports (source, filename, file_sha256, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (source, filename, file_sha256, stamp),
            )
            return int(cur.lastrowid)

    def _drop_import(self, import_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM ledger_imports WHERE id = ?", (import_id,))

    def list_entries(self, limit: int | None = None) -> list[LedgerEntry]:
        sql = """
            SELECT id, amount_kopecks, occurred_at, description, source, consumed_by, import_id
            FROM ledger_entries
            ORDER BY occurred_at DESC, id DESC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def list_coverage(self, limit: int | None = None) -> list[Coverage]:
        sql = """
            SELECT id, start_at, end_at, source, import_id
            FROM ledger_coverage
            ORDER BY id DESC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Coverage(
                id=int(r["id"]),
                start_at=_parse_iso(r["start_at"]),
                end_at=_parse_iso(r["end_at"]),
                source=r["source"],
                import_id=_optional_int(r["import_id"]),
            )
            for r in rows
        ]

    def match(self, fields: ExtractedFields, doc_hash: str | None = None) -> MatchResult:
        if not fields.usable_for_match:
            return MatchResult(covered=self._covered(fields.occurred_at) is not None)
        occurred = fields.occurred_at
        assert occurred is not None
        coverage = self._covered(occurred)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, amount_kopecks, occurred_at, description, source, consumed_by, import_id
                FROM ledger_entries
                WHERE amount_kopecks = ?
                """,
                (fields.amount_kopecks,),
            ).fetchall()
        candidates = [
            e
            for e in (_row_to_entry(r) for r in rows)
            if abs(e.occurred_at - occurred) <= MATCH_WINDOW
        ]
        if not candidates:
            return MatchResult(covered=coverage is not None, coverage=coverage)

        def closeness(entry: LedgerEntry) -> timedelta:
            delta = entry.occurred_at - occurred
            return abs(delta)

        unconsumed = [e for e in candidates if not e.consumed_by]
        mine = [e for e in candidates if doc_hash and e.consumed_by == doc_hash]
        others = [e for e in candidates if e.consumed_by and e.consumed_by != doc_hash]
        if unconsumed:
            hit = min(unconsumed, key=closeness)
            return MatchResult(hit=hit, covered=coverage is not None, coverage=coverage)
        if mine:
            hit = min(mine, key=closeness)
            return MatchResult(hit=hit, covered=coverage is not None, coverage=coverage)
        hit = min(others, key=closeness)
        return MatchResult(hit=hit, covered=coverage is not None, reused=True, coverage=coverage)

    def _covered(self, occurred: datetime | None) -> Coverage | None:
        if occurred is None:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, start_at, end_at, source, import_id FROM ledger_coverage"
            ).fetchall()
        for row in rows:
            cov = Coverage(
                id=int(row["id"]),
                start_at=_parse_iso(row["start_at"]),
                end_at=_parse_iso(row["end_at"]),
                source=row["source"],
                import_id=_optional_int(row["import_id"]),
            )
            if cov.start_at <= occurred <= cov.end_at:
                return cov
        return None


def match_signals(
    match: MatchResult,
    fields: ExtractedFields,
    *,
    direction: str = "unknown",
) -> list[Signal]:
    signals: list[Signal] = []
    if match.confirmed and match.hit is not None:
        signals.append(
            Signal(
                id="ledger_match",
                passed=True,
                severity=Severity.INFO,
                explanation=(
                    f"На счёте есть поступление {format_rub(match.hit.amount_kopecks)} "
                    f"за {format_dt(match.hit.occurred_at)}"
                ),
                evidence={
                    "entry_id": match.hit.id,
                    "source": match.hit.source,
                    "delta_seconds": int(
                        abs(
                            (match.hit.occurred_at - fields.occurred_at).total_seconds()
                        )
                    )
                    if fields.occurred_at
                    else None,
                },
            )
        )
        return signals
    if match.reused and match.hit is not None:
        signals.append(
            Signal(
                id="ledger_reuse",
                passed=False,
                severity=Severity.CRITICAL,
                explanation=(
                    "Это поступление уже подтверждало другой документ — "
                    "повторное использование одного платежа"
                ),
                evidence={"entry_id": match.hit.id, "consumed_by": match.hit.consumed_by},
            )
        )
        return signals
    if direction == "outgoing":
        signals.append(
            Signal(
                id="ledger_outgoing",
                passed=False,
                severity=Severity.INFO,
                explanation=(
                    "Документ похож на чек отправителя (исходящий перевод). "
                    "Журнал сверяет только полученные деньги, поэтому совпадения нет — "
                    "это не доказательство подделки"
                ),
            )
        )
        return signals
    amount = (
        format_rub(fields.amount_kopecks)
        if fields.amount_kopecks is not None
        else "указанную сумму"
    )
    when = format_dt(fields.occurred_at) if fields.occurred_at else "указанную дату"
    if match.covered and match.coverage is not None:
        signals.append(
            Signal(
                id="ledger_missing",
                passed=False,
                severity=Severity.INFO,
                explanation=(
                    f"В журнале поступлений за {format_dt(match.coverage.start_at)}–"
                    f"{format_dt(match.coverage.end_at)} нет {amount} около {when}. "
                    "Структура документа проверена отдельно — отсутствие в выписке "
                    "само по себе не делает файл подделкой"
                ),
                evidence={
                    "coverage_start": match.coverage.start_at.isoformat(),
                    "coverage_end": match.coverage.end_at.isoformat(),
                },
            )
        )
        return signals
    if fields.usable_for_match:
        signals.append(
            Signal(
                id="ledger_missing",
                passed=False,
                severity=Severity.INFO,
                explanation=(
                    f"Совпадения с журналом поступлений нет ({amount} около {when})"
                ),
            )
        )
    return signals


def _already_have_same_day(
    amount_kopecks: int,
    occurred_at: datetime,
    existing: Counter[tuple[int, date]],
    seen: Counter[tuple[int, date]],
) -> bool:
    """Пропуск, если такая сумма в этот день уже есть в нужном количестве.

    Два разных зачисления одной суммы в один день из одной выписки оба
    остаются. Повтор той же оплаты из CSV и PDF не создаёт третью запись.
    """
    key = (amount_kopecks, occurred_at.date())
    seen[key] += 1
    return existing[key] >= seen[key]


def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        id=int(row["id"]),
        amount_kopecks=int(row["amount_kopecks"]),
        occurred_at=_parse_iso(row["occurred_at"]),
        description=row["description"] or "",
        source=row["source"],
        consumed_by=row["consumed_by"],
        import_id=_optional_int(row["import_id"]) if "import_id" in row.keys() else None,
    )


def _row_to_import(row: sqlite3.Row) -> LedgerImport:
    return LedgerImport(
        id=int(row["id"]),
        source=row["source"],
        filename=row["filename"] or "",
        file_sha256=row["file_sha256"],
        created_at=_parse_iso(row["created_at"]),
        entry_count=int(row["entry_count"]),
        coverage_count=int(row["coverage_count"]),
        consumed_count=int(row["consumed_count"]),
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _fingerprint(data: bytes | str) -> str:
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _backfill_legacy_imports(conn: sqlite3.Connection) -> None:
    """Старые CSV/PDF без метки файла — одна загрузка на источник, чтобы их можно было убрать."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for source in ("csv", "statement-pdf"):
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM ledger_entries
            WHERE import_id IS NULL AND source = ?
            """,
            (source,),
        ).fetchone()[0]
        pending_cov = conn.execute(
            """
            SELECT COUNT(*) FROM ledger_coverage
            WHERE import_id IS NULL AND source = ?
            """,
            (source,),
        ).fetchone()[0]
        if not pending and not pending_cov:
            continue
        cur = conn.execute(
            """
            INSERT INTO ledger_imports (source, filename, file_sha256, created_at)
            VALUES (?, ?, NULL, ?)
            """,
            (source, "загрузка до учёта файлов", stamp),
        )
        import_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE ledger_entries SET import_id = ? WHERE import_id IS NULL AND source = ?",
            (import_id, source),
        )
        conn.execute(
            "UPDATE ledger_coverage SET import_id = ? WHERE import_id IS NULL AND source = ?",
            (import_id, source),
        )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _looks_like_header(header: list[str]) -> bool:
    blob = " ".join(header)
    return any(word in blob for word in ("дата", "date", "сумма", "amount"))


def _header_index(header: list[str], names: tuple[str, ...]) -> int | None:
    for i, cell in enumerate(header):
        if any(name in cell for name in names):
            return i
    return None


def _amount_column_index(header: list[str]) -> int | None:
    for i, cell in enumerate(header):
        if "сумма" in cell and ("сч" in cell or "карт" in cell):
            return i
    return _header_index(header, ("сумма", "amount", "sum"))


def _csv_is_credit(signed_amount: int, direction_cell: str) -> bool:
    blob = direction_cell.lower().replace("ё", "е")
    if "списание" in blob and "зачисление" not in blob:
        return False
    if "зачисление" in blob or "приход" in blob:
        return True
    if "debit" in blob:
        return False
    if "credit" in blob:
        return True
    return signed_amount > 0


def _csv_is_completed(status_cell: str) -> bool:
    if not status_cell.strip():
        return True
    blob = status_cell.lower().replace("ё", "е")
    if any(word in blob for word in ("отклон", "отмен", "error", "fail")):
        return False
    if any(word in blob for word in ("обработк", "ожид", "pending", "hold")):
        return False
    return True


def _intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start <= b_end and b_start <= a_end


