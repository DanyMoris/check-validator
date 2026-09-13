"""Собирает resources/fraudulent/: те же страницы, что у банка, чужие метаданные.

Страница не перерисовывается. В Info пишется чужой Producer/Creator — так выглядит
файл, который открыли и сохранили в Acrobat или «печать в PDF». Детектор в режиме
без выписок должен ответить ПОДДЕЛКА.

Запуск:  .venv\\Scripts\\python.exe tools\\populate_fraudulent.py
Нужны образцы в resources/genuine/. PDF в git не попадают (персональные данные).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import pikepdf  # noqa: E402

from checkvalidator.engine import analyse  # noqa: E402
from checkvalidator.fingerprint import fingerprint  # noqa: E402
from checkvalidator.models import CheckMode, Verdict  # noqa: E402
from checkvalidator.profiles import ProfileRegistry  # noqa: E402

GENUINE = ROOT / "resources" / "genuine"
FRAUD = ROOT / "resources" / "fraudulent"

# По два файла на банк: разные чужие программы в метаданных.
STAMPS = (
    ("acrobat", "Acrobat Distiller 25.0 Windows", "Adobe Acrobat Pro"),
    ("msprint", "Microsoft: Print To PDF", "Microsoft Word"),
)

SOURCES: list[tuple[str, str]] = [
    ("VTB", "Чек"),
    ("SBER", "Чек"),
    ("ALFA", "Квитанция"),
    ("TBANK", "Квитанция"),
]


def _pdf_date(when: datetime) -> str:
    return when.strftime("D:%Y%m%d%H%M%S+00'00'")


def _pick(bank: str, doc_type: str, n: int) -> list[Path]:
    folder = GENUINE / bank / doc_type
    files = sorted(
        p
        for p in folder.glob("*.pdf")
        if "out" not in p.name.lower()
    )
    return files[:n]


def stamp_metadata(src: Path, dst: Path, producer: str, creator: str) -> None:
    """Меняет только служебные поля. Содержимое страницы не нормализуется."""
    now = datetime.now(timezone.utc)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(src) as pdf:
        info = pdf.docinfo
        info["/Producer"] = producer
        info["/Creator"] = creator
        info["/ModDate"] = _pdf_date(now)
        pdf.save(dst)


def main() -> int:
    registry = ProfileRegistry.load()
    if FRAUD.exists():
        for pdf in FRAUD.rglob("*.pdf"):
            pdf.unlink()

    made = 0
    failed = 0
    for bank, doc_type in SOURCES:
        picked = _pick(bank, doc_type, 2)
        if len(picked) < 2:
            print(f"{bank}/{doc_type}: мало образцов ({len(picked)})")
            failed += 1
            continue
        for src, (tag, producer, creator) in zip(picked, STAMPS, strict=True):
            dst = FRAUD / bank / doc_type / f"{tag}_{src.name}"
            stamp_metadata(src, dst, producer, creator)
            fp = fingerprint(dst)
            report = analyse(
                dst, registry, expected=f"{bank}/{doc_type}", mode=CheckMode.STRUCTURE
            )
            ok = report.verdict is Verdict.FORGED
            print(
                f"{dst.relative_to(FRAUD).as_posix()}: "
                f"producer={fp.producer!r} verdict={report.verdict.value} "
                f"{'ok' if ok else 'MISS'}"
            )
            if not ok:
                failed += 1
            made += 1

    print(f"записано: {made}  промахов детекции: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
