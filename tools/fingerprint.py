"""Показывает структурный «отпечаток» PDF-файлов корпуса.

Читает только служебную структуру файла — содержимое документов не разбирается.
Заготовка для профилей банков из фазы 2.

Запуск:  .venv\\Scripts\\python.exe tools\\fingerprint.py [путь ...]
Без аргументов обходит весь resources/genuine.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
RE_PRODUCER = re.compile(rb"/Producer\s*\(([^)]{0,60})\)")
RE_OBJ = re.compile(rb"\b\d+\s+0\s+obj\b")
RE_FONT = re.compile(rb"/BaseFont\s*/([A-Za-z0-9+\-,._]+)")
RE_SUBTYPE = re.compile(rb"/Subtype\s*/(\w+)")


def fingerprint(path: Path) -> dict:
    data = path.read_bytes()
    producer = RE_PRODUCER.search(data)
    return {
        "version": data[5:8].decode("latin-1", "replace"),
        "objects": len(RE_OBJ.findall(data)),
        "xrefs": data.count(b"startxref"),
        "has_prev": b"/Prev" in data,
        "encrypted": b"/Encrypt" in data,
        "producer": producer.group(1).decode("latin-1") if producer else "-",
        "fonts": tuple(sorted({f.decode().split("+")[-1] for f in RE_FONT.findall(data)})),
        "subtypes": tuple(sorted({s.decode() for s in RE_SUBTYPE.findall(data)})),
        "has_image": b"/Image" in data,
        "size": len(data),
    }


def group_key(fp: dict) -> tuple:
    """Признаки, которые должны совпадать у документов одного шаблона."""
    return (fp["version"], fp["objects"], fp["producer"], fp["fonts"], fp["subtypes"])


def main() -> None:
    args = sys.argv[1:]
    paths = (
        [Path(a) for a in args]
        if args
        else sorted((ROOT / "resources" / "genuine").rglob("*.pdf"))
    )
    if not paths:
        sys.exit("PDF-файлы не найдены")

    groups: dict[tuple, list[Path]] = defaultdict(list)
    prints: dict[Path, dict] = {}
    for p in paths:
        fp = fingerprint(p)
        prints[p] = fp
        groups[(p.parent.as_posix(), group_key(fp))].append(p)

    for (folder, _), files in sorted(groups.items()):
        fp = prints[files[0]]
        label = "/".join(Path(folder).parts[-2:])
        sizes = sorted(prints[f]["size"] for f in files)
        print(f"\n{label}  ({len(files)} файл(ов))")
        print(f"   версия PDF   {fp['version']}")
        print(f"   объектов     {fp['objects']}")
        print(f"   producer     {fp['producer']}")
        print(f"   шрифты       {', '.join(fp['fonts']) or '-'}")
        print(f"   subtypes     {', '.join(fp['subtypes'])}")
        print(f"   картинки     {'ЕСТЬ' if fp['has_image'] else 'нет'}")
        print(f"   правки после создания  {'ЕСТЬ' if fp['has_prev'] or fp['xrefs'] > 1 else 'нет'}")
        print(f"   размер       {sizes[0]}..{sizes[-1]} байт")

    print("\n" + "-" * 60)
    for (folder, _), files in sorted(groups.items()):
        label = "/".join(Path(folder).parts[-2:])
        print(f"{label:<28} {len(files)} файл(ов), отпечаток единый")
    if len({k[1] for k in groups}) < len(groups):
        print("\nВНИМАНИЕ: одинаковый отпечаток встречается у разных типов документов.")


if __name__ == "__main__":
    main()
