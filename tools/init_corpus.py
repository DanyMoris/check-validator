"""Приводит папку resources/ к каноническому виду.

Создаёт resources/{genuine,fraudulent}/<БАНК>/<ТИП ДОКУМЕНТА>/ и убирает пустые
папки, не входящие в канон. Папку с файлами скрипт не удаляет никогда.

Запуск:  .venv\\Scripts\\python.exe tools\\init_corpus.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent / "resources"
KINDS = ("genuine", "fraudulent")
BANKS = ("VTB", "SBER", "ALFA", "TBANK")
DOC_TYPES = ("Чек", "Квитанция", "Справка", "Выписка")


def is_empty(d: Path) -> bool:
    """Пусто = нет ничего, кроме .gitkeep."""
    return all(p.name == ".gitkeep" for p in d.iterdir())


def main() -> None:
    created: list[Path] = []
    removed: list[Path] = []
    kept: list[tuple[Path, int]] = []

    for kind in KINDS:
        for bank in BANKS:
            bank_dir = ROOT / kind / bank
            bank_dir.mkdir(parents=True, exist_ok=True)

            for doc_type in DOC_TYPES:
                d = bank_dir / doc_type
                if not d.exists():
                    d.mkdir()
                    created.append(d)
                if is_empty(d):
                    (d / ".gitkeep").touch()
                else:
                    for stale in d.glob(".gitkeep"):
                        stale.unlink()

            # Всё, что не входит в канон: пустое — удаляем, с файлами — оставляем.
            for d in sorted(p for p in bank_dir.iterdir() if p.is_dir()):
                if d.name in DOC_TYPES:
                    continue
                if is_empty(d):
                    for p in d.iterdir():
                        p.unlink()
                    d.rmdir()
                    removed.append(d)
                else:
                    kept.append((d, len(list(d.glob("*.pdf")))))

            for stray in bank_dir.glob(".gitkeep"):
                stray.unlink()

    def rel(p: Path) -> str:
        return p.relative_to(ROOT.parent).as_posix()

    print(f"создано папок:  {len(created)}")
    print(f"удалено пустых: {len(removed)}")
    if kept:
        print("\nНЕ УДАЛЕНО (в папке есть файлы, разберите вручную):")
        for d, n in kept:
            print(f"  {rel(d)}  — {n} pdf")

    print("\nТекущее наполнение:")
    total = 0
    for kind in KINDS:
        for bank in BANKS:
            for doc_type in DOC_TYPES:
                d = ROOT / kind / bank / doc_type
                n = len(list(d.glob("*.pdf")))
                total += n
                if n:
                    print(f"  {rel(d):<44} {n}")
    print(f"\nвсего документов: {total}")


if __name__ == "__main__":
    main()
