"""Изготавливает поддельные документы из настоящих — для испытаний детектора.

Настоящих подделок на руках нет, поэтому пороги проверок настраиваются на
смоделированных атаках. Это заведомо слабее живых образцов: здесь воспроизведены
только типовые способы, которыми документ портят на практике. Когда появятся
настоящие подделки, проверки нужно перенастроить на них.

Запуск:  .venv\\Scripts\\python.exe tools\\forge.py
Результат: build/forged/<АТАКА>/...
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

GENUINE = ROOT / "resources" / "genuine"
OUT = ROOT / "build" / "forged"


def attack_resave(src: Path, dst: Path) -> str:
    """Круговой пересохран без изменений — самый трудный случай.

    pikepdf сохраняет исходную строку producer и почти всю структуру, так что
    отличить результат от оригинала по одному только устройству файла нельзя.
    Атака оставлена в наборе намеренно: она показывает границу метода, а не
    служит для накрутки процента обнаружения.
    """
    import pikepdf

    with pikepdf.open(src) as pdf:
        pdf.save(dst)
    return "пересохранён с сохранением структуры"


def attack_office_resave(src: Path, dst: Path) -> str:
    """Пересохран программой, которая переписывает файл по-своему.

    Так выглядит документ, побывавший в Word, Acrobat или онлайн-конвертере:
    содержимое похоже, а служебная часть уже чужая.
    """
    import pikepdf

    with pikepdf.open(src) as pdf:
        pdf.docinfo["/Producer"] = "Microsoft: Print To PDF"
        pdf.docinfo["/Creator"] = "Microsoft Word"
        pdf.save(dst, normalize_content=True)
    return "пересохранён офисной программой"


def attack_producer_spoof(src: Path, dst: Path) -> str:
    """Пересохранить, но подставить в метаданные банковский producer.
    Проверяет, что вердикт не держится на одном этом признаке."""
    import pikepdf

    with pikepdf.open(src) as pdf:
        original = str(pdf.docinfo.get("/Producer", ""))
        pdf.docinfo["/Producer"] = original or "openhtmltopdf.com"
        pdf.save(dst)
    return "пересохранён с подделанной строкой producer"


def attack_append(src: Path, dst: Path) -> str:
    """Дописать ревизию в готовый файл — так выглядит правка задним числом."""
    data = src.read_bytes()
    tail = (
        b"\n%% appended revision\n"
        b"trailer\n<< /Prev 0 /Root 1 0 R /Size 1 >>\nstartxref\n0\n%%EOF\n"
    )
    dst.write_bytes(data + tail)
    return "в готовый файл дописана лишняя ревизия"


def attack_rasterise(src: Path, dst: Path) -> str:
    """Отрисовать в картинку и завернуть обратно в PDF. Так поступают, когда
    правят документ в графическом редакторе."""
    import pypdfium2 as pdfium
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    doc = pdfium.PdfDocument(src)
    page = doc[0]
    img = page.render(scale=2).to_pil()
    w, h = page.get_width(), page.get_height()
    c = canvas.Canvas(str(dst), pagesize=(w, h))
    c.drawImage(ImageReader(img), 0, 0, width=w, height=h)
    c.save()
    return "страница превращена в картинку и заново собрана в PDF"


ATTACKS = {
    "resave": attack_resave,
    "office_resave": attack_office_resave,
    "producer_spoof": attack_producer_spoof,
    "append": attack_append,
    "rasterise": attack_rasterise,
}

HARD_BY_DESIGN = {"resave", "producer_spoof"}
"""Атаки, сохраняющие структуру. Структурный анализ их не берёт — это его предел,
а не недоработка. Против них работает только сверка с поступлением денег."""


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)

    sources = sorted(GENUINE.rglob("*.pdf"))
    if not sources:
        sys.exit(f"Нет образцов в {GENUINE}")

    made = 0
    failed: list[str] = []
    for name, fn in ATTACKS.items():
        for src in sources:
            rel = src.relative_to(GENUINE)
            dst = OUT / name / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                fn(src, dst)
                made += 1
            except Exception as exc:
                failed.append(f"{name}/{rel.as_posix()}: {type(exc).__name__}: {exc}")

    print(f"Изготовлено подделок: {made}  ->  {OUT.relative_to(ROOT).as_posix()}")
    print(f"Из образцов: {len(sources)}   видов атак: {len(ATTACKS)}\n")
    for name, fn in ATTACKS.items():
        print(f"  {name:<16} {(fn.__doc__ or '').strip().splitlines()[0]}")
    if failed:
        print("\nНе удалось изготовить:")
        for f in failed:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
