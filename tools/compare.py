"""Показывает, чем отпечатки нескольких файлов отличаются друг от друга.

Нужен при разборе: почему подделка прошла проверку, или почему настоящий
документ её не прошёл.

Запуск:  .venv\\Scripts\\python.exe tools\\compare.py файл1.pdf файл2.pdf ...
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from checkvalidator.fingerprint import fingerprint  # noqa: E402

FIELDS = (
    "pdf_version",
    "objects",
    "revisions",
    "producer",
    "creator",
    "fonts",
    "subtypes",
    "has_image",
    "encrypted",
    "size",
)


def main() -> None:
    paths = [Path(a) for a in sys.argv[1:]]
    if len(paths) < 2:
        sys.exit("Нужно минимум два файла")

    fps = [fingerprint(p) for p in paths]

    print("файлы:")
    for i, p in enumerate(paths):
        print(f"  [{i}] {p}")
    print()

    for field in FIELDS:
        values = [getattr(fp, field) for fp in fps]
        same = all(v == values[0] for v in values)
        marker = "   " if same else " ! "
        print(f"{marker}{field}")
        if same:
            print(f"      всё одинаково: {values[0]}")
        else:
            for i, v in enumerate(values):
                print(f"      [{i}] {v}")

    sigs = [fp.signature for fp in fps]
    if any(s.present for s in sigs):
        print("\n   подпись")
        for i, s in enumerate(sigs):
            state = f"есть, {s.signature_algorithm}" if s.present else "нет"
            cover = "" if s.covers_whole_file is None else f", покрывает файл: {s.covers_whole_file}"
            err = f", ошибка разбора: {s.parse_error}" if s.parse_error else ""
            print(f"      [{i}] {state}{cover}{err}")


if __name__ == "__main__":
    main()
