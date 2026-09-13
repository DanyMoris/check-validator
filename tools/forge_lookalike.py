"""Подделки «как живой чек»: тот же шаблон банка, подменена только сумма.

Не пересохраняем через Word и не рисуем картинку — страница остаётся банковским
PDF. Так выглядит правка в исходнике, а не наивный скриншот. Структурный
детектор такие файлы обычно не объявляет подделкой: против них нужен журнал.

Запуск:  .venv\\Scripts\\python.exe tools\\forge_lookalike.py
Результат: demo/lookalike_*.pdf
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import pikepdf  # noqa: E402

from checkvalidator.engine import analyse  # noqa: E402
from checkvalidator.extract import extract  # noqa: E402
from checkvalidator.fingerprint import fingerprint  # noqa: E402
from checkvalidator.profiles import ProfileRegistry  # noqa: E402

GENUINE = ROOT / "resources" / "genuine"
DEMO = ROOT / "demo"

# Чеки с короткими латинскими именами — проще отдавать заказчику.
SOURCES = [
    GENUINE / "VTB" / "Чек" / "02-09-26_15-02.pdf",
    GENUINE / "VTB" / "Чек" / "11-09-26_15-19.pdf",
    GENUINE / "VTB" / "Чек" / "12-09-26_20-32.pdf",
]


def _parse_cmap(raw: bytes) -> dict[int, str]:
    cid_to_char: dict[int, str] = {}
    text = raw.decode("latin-1", errors="replace")
    for m in re.finditer(
        r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", text
    ):
        lo, hi, dst = (int(m.group(i), 16) for i in (1, 2, 3))
        if hi < lo:
            continue
        for i in range(hi - lo + 1):
            cid_to_char[lo + i] = chr(dst + i)
    for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", text):
        cid, uni = int(m.group(1), 16), int(m.group(2), 16)
        cid_to_char.setdefault(cid, chr(uni))
    return cid_to_char


def _fonts_cmaps(pdf: pikepdf.Pdf) -> list[dict[int, str]]:
    maps: list[dict[int, str]] = []
    seen: set[int] = set()

    def add_from_resources(res) -> None:
        if res is None:
            return
        fonts = res.get("/Font", {}) if hasattr(res, "get") else {}
        for font in fonts.values():
            tu = font.get("/ToUnicode") if hasattr(font, "get") else None
            if tu is None:
                continue
            ident = id(tu)
            if ident in seen:
                continue
            seen.add(ident)
            try:
                cmap = _parse_cmap(tu.read_bytes())
            except Exception:
                continue
            if cmap:
                maps.append(cmap)
        xobjs = res.get("/XObject", {}) if hasattr(res, "get") else {}
        for xobj in xobjs.values():
            if hasattr(xobj, "get") and xobj.get("/Subtype") == "/Form":
                add_from_resources(xobj.get("/Resources"))

    for page in pdf.pages:
        add_from_resources(page.get("/Resources", {}))
    return maps


def _digit_cids(cmap: dict[int, str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for cid, ch in cmap.items():
        if ch.isdigit() and ch not in out:
            width = 2 if cid > 255 else 1
            out[ch] = cid.to_bytes(width, "big")
    # Identity-H almost always uses 2-byte CIDs even for < 256.
    if any(ch in cmap.values() for ch in "0123456789"):
        two: dict[str, bytes] = {}
        for cid, ch in cmap.items():
            if ch.isdigit() and ch not in two:
                two[ch] = cid.to_bytes(2, "big")
        if len(two) >= 10:
            return two
    return out


def _ruble_digits(kopecks: int) -> str:
    """Цифры целых рублей. Копейки вроде 1,40 не сводим к одной цифре «1»."""
    rubles, kop = divmod(kopecks, 100)
    if rubles == 0 and kop:
        return f"{kop:02d}"
    return str(rubles)


def _alt_digits(digits: str) -> str:
    """Та же длина, другие цифры. Не начинаем с нуля."""
    table = str.maketrans("0123456789", "9876543210")
    alt = digits.translate(table)
    if alt[0] == "0":
        alt = "9" + alt[1:]
    if alt == digits:
        alt = ("9" * len(digits)) if digits[0] != "9" else ("8" * len(digits))
    return alt


def _decode_tj(chunk: bytes, cmap: dict[int, str]) -> str:
    chars: list[str] = []
    for sm in re.finditer(rb"\((.{1,8})\)", chunk, re.DOTALL):
        raw = sm.group(1)
        if len(raw) in (1, 2):
            cid = int.from_bytes(raw, "big")
            chars.append(cmap.get(cid, ""))
    if chars:
        return "".join(chars)
    try:
        return chunk.decode("latin-1")
    except Exception:
        return ""


def _replace_digit_run(data: bytes, old: list[bytes], new: list[bytes]) -> tuple[bytes, int]:
    """Меняет цифры суммы внутри одного TJ, где они стоят рядом (с кернингом)."""
    if not old or len(old) != len(new):
        return data, 0
    pos = 0
    hits: list[tuple[int, int]] = []
    for code in old:
        found = data.find(code, pos)
        if found < 0 or found - pos > 80:
            return data, 0
        hits.append((found, len(code)))
        pos = found + len(code)
    out = bytearray(data)
    for (start, length), repl in zip(reversed(hits), reversed(new)):
        if len(repl) != length:
            return data, 0
        out[start : start + length] = repl
    return bytes(out), 1


def _patch_stream(data: bytes, cmap: dict[int, str], old_digits: str, new_digits: str) -> tuple[bytes, int]:
    dmap = _digit_cids(cmap)
    if not all(ch in dmap for ch in old_digits + new_digits):
        ascii_old, ascii_new = old_digits.encode(), new_digits.encode()
        if ascii_old in data and len(ascii_old) == len(ascii_new):
            return data.replace(ascii_old, ascii_new, 1), 1
        return data, 0
    old_codes = [dmap[ch] for ch in old_digits]
    new_codes = [dmap[ch] for ch in new_digits]
    out = bytearray(data)
    changed = 0
    for match in re.finditer(rb"\[.*?\]\s*TJ", data, re.DOTALL):
        chunk = match.group(0)
        shown = _decode_tj(chunk, cmap)
        compact = re.sub(r"\D", "", shown)
        if compact != old_digits and old_digits not in shown:
            continue
        patched, n = _replace_digit_run(chunk, old_codes, new_codes)
        if n:
            out[match.start() : match.end()] = patched
            changed += n
            break
    if changed:
        return bytes(out), changed
    ascii_old, ascii_new = old_digits.encode(), new_digits.encode()
    if ascii_old in data and len(ascii_old) == len(ascii_new):
        return data.replace(ascii_old, ascii_new, 1), 1
    return data, 0


def _patch_pdf(src: Path, dst: Path, new_digits: str) -> bool:
    fields = extract(src)
    if fields.amount_kopecks is None:
        return False
    old_digits = _ruble_digits(fields.amount_kopecks)
    if len(old_digits) != len(new_digits):
        return False

    with pikepdf.open(src) as pdf:
        cmaps = _fonts_cmaps(pdf)
        mapping = None
        cmap_used = None
        for cmap in cmaps:
            dmap = _digit_cids(cmap)
            if all(ch in dmap for ch in old_digits + new_digits):
                mapping = dmap
                cmap_used = cmap
                break
        if cmap_used is None and cmaps:
            cmap_used = cmaps[0]
        if cmap_used is None:
            cmap_used = {}
        patched = 0
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            try:
                data = obj.read_bytes()
            except Exception:
                continue
            if b"TJ" not in data and b"Tj" not in data and old_digits.encode() not in data:
                continue
            updated, n = _patch_stream(data, cmap_used, old_digits, new_digits)
            if n:
                obj.write(updated)
                patched += n
                break
        if not patched:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(dst)
    return True


def main() -> int:
    registry = ProfileRegistry.load()
    DEMO.mkdir(exist_ok=True)
    made = 0
    for src in SOURCES:
        if not src.exists():
            print(f"нет файла: {src.as_posix()}")
            continue
        fields = extract(src)
        if fields.amount_kopecks is None:
            print(f"нет суммы: {src.name}")
            continue
        digits = _ruble_digits(fields.amount_kopecks)
        alt = _alt_digits(digits)
        dst = DEMO / f"lookalike_vtb_{digits}_na_{alt}.pdf"
        ok = _patch_pdf(src, dst, alt)
        if not ok:
            print(f"не удалось подменить сумму: {src.name}")
            continue
        before = fingerprint(src)
        after = fingerprint(dst)
        got = extract(dst)
        report = analyse(dst, registry)
        print(
            f"{dst.name}: old={digits} new_expect={alt} "
            f"extracted={got.amount_kopecks} verdict={report.verdict.value} "
            f"profile={report.profile_id} "
            f"objects {before.objects}->{after.objects} "
            f"producer_same={before.producer == after.producer}"
        )
        made += 1
    print(f"готово: {made}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
